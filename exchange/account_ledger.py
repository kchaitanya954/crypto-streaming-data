"""
What actually happened on CoinDCX in a time window — the source of truth for reports.

The bot's own tables only know about orders the bot placed and estimate exit prices
from candle closes. Exchange-side SL/TP fills, liquidations, funding, manual trades
and positions opened by accident all bypass them. This module reads the exchange's
ledgers instead:

  futures  — positions/transactions: realised P&L (`amount`), fees, funding, per fill
             (USDT- and INR-margined books are reported separately)
  spot     — trade_history fills: volume, fees; realised P&L ≈ FIFO against the
             previous 30 days of fills
  now      — open futures positions (with/without exchange stop-loss) and wallets
"""

import logging
import time
from collections import defaultdict, deque
from typing import Optional

_log = logging.getLogger("account_ledger")

_EXIT_STAGES = {"exit", "tpsl_exit", "liquidate", "liquidation", "default"}


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ms(row: dict) -> int:
    v = row.get("created_at") or row.get("timestamp") or 0
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0
    return int(v if v > 1e11 else v * 1000)


def summarize_futures_transactions(rows: list[dict], from_ms: int, to_ms: int) -> dict:
    """
    Aggregate exchange ledger rows inside [from_ms, to_ms). `amount` on exit-type
    stages is realised P&L for that fill; opening fills carry amount 0 and a fee.
    """
    per = defaultdict(lambda: {"pnl": 0.0, "fees": 0.0, "funding": 0.0, "closes": 0,
                               "wins": 0, "losses": 0, "liquidations": 0, "fills": 0})
    for r in rows:
        t = _ms(r)
        if not (from_ms <= t < to_ms):
            continue
        p = per[r.get("pair", "?")]
        stage = (r.get("stage") or "").lower()
        amount, fee = _f(r, "amount"), _f(r, "fee_amount")
        p["fees"] += fee
        if stage == "funding":
            p["funding"] += amount
            continue
        if stage in _EXIT_STAGES:
            p["fills"] += 1
            p["pnl"] += amount
            if stage in ("liquidate", "liquidation"):
                p["liquidations"] += 1
            if amount:
                p["closes"] += 1
                p["wins" if amount > 0 else "losses"] += 1
    tot = {k: sum(v[k] for v in per.values()) for k in
           ("pnl", "fees", "funding", "closes", "wins", "losses", "liquidations", "fills")}
    tot["net"] = tot["pnl"] - tot["fees"] + tot["funding"]
    for v in per.values():
        v["net"] = v["pnl"] - v["fees"] + v["funding"]
    return {"pairs": dict(per), "total": tot}


def summarize_spot_fills(fills: list[dict], from_ms: int, to_ms: int) -> dict:
    """
    Volume, fees and FIFO-realised P&L for sells inside the window. Fills before
    from_ms only seed the cost basis. Sells with no known cost basis (coins bought
    before the lookback) are counted as unmatched, not as profit.
    """
    lots: dict[str, deque] = defaultdict(deque)
    per = defaultdict(lambda: {"buys": 0, "sells": 0, "volume": 0.0, "fees": 0.0,
                               "realised": 0.0, "unmatched_qty": 0.0})
    for f in sorted(fills, key=_ms):
        t, mkt = _ms(f), f.get("symbol") or f.get("market") or "?"
        q, px, fee = _f(f, "quantity"), _f(f, "price"), _f(f, "fee_amount")
        side = (f.get("side") or "").lower()
        in_win = from_ms <= t < to_ms
        if side == "buy":
            lots[mkt].append([q, px])
        else:
            pnl, left = 0.0, q
            while left > 1e-12 and lots[mkt]:
                lot = lots[mkt][0]
                take = min(left, lot[0])
                pnl += take * (px - lot[1])
                lot[0] -= take
                left -= take
                if lot[0] <= 1e-12:
                    lots[mkt].popleft()
            if in_win:
                per[mkt]["realised"] += pnl
                per[mkt]["unmatched_qty"] += left
        if in_win:
            p = per[mkt]
            p["buys" if side == "buy" else "sells"] += 1
            p["volume"] += q * px
            p["fees"] += fee
    tot = {k: sum(v[k] for v in per.values()) for k in ("buys", "sells", "volume", "fees", "realised")}
    tot["net"] = tot["realised"] - tot["fees"]
    return {"markets": dict(per), "total": tot}


def open_position_rows(positions: list[dict]) -> list[dict]:
    out = []
    for p in positions:
        qty = _f(p, "active_pos")
        if abs(qty) <= 0:
            continue
        avg, mark = _f(p, "avg_price"), _f(p, "mark_price")
        out.append({
            "pair": p.get("pair"), "side": "long" if qty > 0 else "short", "qty": abs(qty),
            "avg": avg, "mark": mark, "upnl": (mark - avg) * qty if mark and avg else 0.0,
            "leverage": p.get("leverage"), "liq": _f(p, "liquidation_price"),
            "sl": _f(p, "stop_loss_trigger") or None, "tp": _f(p, "take_profit_trigger") or None,
            "margin_currency": p.get("margin_currency_short_name") or "USDT", "id": p.get("id"),
        })
    return out


async def _transactions_since(exchange, from_ms: int, margin_currency: str, max_pages: int = 30) -> list[dict]:
    """Newest-first ledger pages until we're past from_ms."""
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        rows = await exchange.get_futures_transactions(page=page, size=100, margin_currency=margin_currency)
        out.extend(rows)
        if len(rows) < 100 or (rows and min(_ms(r) for r in rows) < from_ms):
            break
    return out


async def _spot_fills_between(exchange, from_ms: int, to_ms: int) -> list[dict]:
    out: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(40):
        rows = await exchange.get_spot_fills(from_ms=from_ms, to_ms=to_ms, limit=500, sort="asc", from_id=cursor)
        new = [r for r in rows if str(r.get("id")) != str(cursor)]
        out.extend(new)
        if len(rows) < 500 or not new:
            break
        cursor = new[-1].get("id")
    return out


async def exchange_window_summary(exchange, from_ts: int, to_ts: int) -> dict:
    """
    Everything the daily report needs from the exchange for [from_ts, to_ts) (Unix s).
    Each section fails independently — a flaky endpoint shows up as an error line,
    it never hides the rest of the report.
    """
    from_ms, to_ms = from_ts * 1000, to_ts * 1000
    out: dict = {"errors": []}

    for cur in ("USDT", "INR"):
        try:
            rows = await _transactions_since(exchange, from_ms, cur)
            out[f"futures_{cur.lower()}"] = summarize_futures_transactions(rows, from_ms, to_ms)
        except Exception as exc:
            out["errors"].append(f"futures {cur} ledger: {str(exc)[:120]}")

    try:
        fills = await _spot_fills_between(exchange, from_ms - 30 * 86_400_000, to_ms)
        out["spot"] = summarize_spot_fills(fills, from_ms, to_ms)
    except Exception as exc:
        out["errors"].append(f"spot fills: {str(exc)[:120]}")

    positions: list[dict] = []
    for cur in ("USDT", "INR"):
        try:
            positions += await exchange.list_futures_positions(cur)
        except Exception as exc:
            out["errors"].append(f"positions {cur}: {str(exc)[:120]}")
    out["open_positions"] = open_position_rows(positions)

    try:
        out["wallets"] = {w.get("currency_short_name"): _f(w, "balance") + _f(w, "locked_balance")
                          for w in await exchange.get_futures_wallets()}
    except Exception as exc:
        out["errors"].append(f"futures wallets: {str(exc)[:120]}")

    out["generated_at"] = int(time.time())
    return out
