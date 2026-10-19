"""
Read-only CoinDCX account audit — what actually happened on the exchange.

Pulls futures fills, futures position transactions (the exchange's own realised
P&L, fees and funding) and spot fills, then prints a per-pair summary. It only
calls read endpoints; it never places, edits or cancels anything.

Usage (keys come from .env or the environment, same names the bot uses):
    python -m scripts.coindcx_audit            # last 14 days
    python -m scripts.coindcx_audit --days 30
    python -m scripts.coindcx_audit --raw      # also dump raw rows as JSON
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from exchange.coindcx_client import CoinDCXClient  # noqa: E402

FUT = "/exchange/v1/derivatives/futures"


def _rows(result) -> list:
    """Endpoints return either a bare list or a dict wrapping one."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in ("data", "trades", "transactions", "orders", "positions"):
            if isinstance(result.get(key), list):
                return result[key]
    return []


def _f(row: dict, *keys, default=0.0) -> float:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def _ts(row: dict) -> float:
    """created_at / timestamp may be ms, seconds or ISO text — return seconds."""
    v = row.get("created_at") or row.get("timestamp") or row.get("updated_at")
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else v
    if isinstance(v, str):
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


async def _paged(client, path: str, body: dict, pages: int = 20, size: int = 100) -> list:
    out = []
    for page in range(1, pages + 1):
        b = dict(body, timestamp=int(time.time() * 1000), page=str(page), size=str(size))
        rows = _rows(await client._post(path, b))
        out.extend(rows)
        if len(rows) < size:
            break
    return out


async def _section(name: str, coro):
    try:
        return await coro
    except Exception as exc:  # report and keep going — one bad endpoint shouldn't hide the rest
        print(f"  ! {name} failed: {str(exc)[:300]}")
        return []


async def main(days: int, raw: bool) -> None:
    load_dotenv()
    key, secret = os.getenv("COINDCX_API_KEY"), os.getenv("COINDCX_API_SECRET")
    if not key or not secret:
        sys.exit("Set COINDCX_API_KEY and COINDCX_API_SECRET in .env or the environment.")

    since = time.time() - days * 86400
    today = dt.date.today()
    from_date = (today - dt.timedelta(days=days)).isoformat()
    to_date = (today + dt.timedelta(days=1)).isoformat()
    usdt = {"margin_currency_short_name": ["USDT"]}

    async with aiohttp.ClientSession() as session:
        c = CoinDCXClient(api_key=key, api_secret=secret, session=session)
        print(f"CoinDCX audit — last {days} days ({from_date} → {today})\n")

        txns = await _section("futures transactions", _paged(c, f"{FUT}/positions/transactions", {"stage": "all", **usdt}))
        fills = await _section("futures trades", _paged(c, f"{FUT}/trades", {"from_date": from_date, "to_date": to_date, **usdt}))
        positions = await _section("futures positions", _paged(c, f"{FUT}/positions", usdt, pages=2))
        spot = await _section("spot trade history", c.get_trade_history(limit=500))

    txns = [t for t in txns if _ts(t) >= since]
    fills = [t for t in fills if _ts(t) >= since]
    spot = [t for t in spot if _ts(t) >= since]

    # ── Futures: exchange-reported realised P&L, fees, funding ────────────────
    print("FUTURES — realised P&L as reported by CoinDCX (positions/transactions)")
    per = defaultdict(lambda: {"pnl": 0.0, "fees": 0.0, "funding": 0.0, "wins": 0, "losses": 0, "liq": 0})
    for t in txns:
        p = per[t.get("pair", "?")]
        amount, fee = _f(t, "amount"), _f(t, "fee_amount")
        stage = (t.get("stage") or "").lower()
        p["fees"] += fee
        if stage == "funding":
            p["funding"] += amount
        elif stage in ("exit", "tpsl_exit", "liquidate", "default"):
            p["pnl"] += amount
            if stage == "liquidate":
                p["liq"] += 1
            if stage != "default" or amount:
                p["wins" if amount > 0 else "losses"] += 1
    if not per:
        print("  (no transactions returned)")
    tot = defaultdict(float)
    for pair, p in sorted(per.items()):
        net = p["pnl"] - p["fees"] + p["funding"]
        n = p["wins"] + p["losses"]
        wr = f"{p['wins'] / n * 100:.0f}%" if n else "—"
        print(f"  {pair:<14} closes={n:<4} win={wr:<5} pnl={p['pnl']:+.4f}  fees=-{p['fees']:.4f}  "
              f"funding={p['funding']:+.4f}  NET={net:+.4f} USDT" + (f"  LIQUIDATIONS={p['liq']}" if p["liq"] else ""))
        for k in ("pnl", "fees", "funding"):
            tot[k] += p[k]
    if per:
        print(f"  {'TOTAL':<14} pnl={tot['pnl']:+.4f}  fees=-{tot['fees']:.4f}  funding={tot['funding']:+.4f}  "
              f"NET={tot['pnl'] - tot['fees'] + tot['funding']:+.4f} USDT")
    stages = defaultdict(int)
    for t in txns:
        stages[t.get("stage")] += 1
    print(f"  transaction stages seen: {dict(stages)}")

    # ── Futures fills: activity and fee drag ──────────────────────────────────
    print(f"\nFUTURES FILLS — {len(fills)} fills")
    fp = defaultdict(lambda: {"n": 0, "notional": 0.0, "fees": 0.0, "maker": 0})
    for t in fills:
        p = fp[t.get("pair", "?")]
        p["n"] += 1
        p["notional"] += _f(t, "price") * _f(t, "quantity")
        p["fees"] += _f(t, "fee_amount")
        p["maker"] += 1 if t.get("is_maker") else 0
    for pair, p in sorted(fp.items()):
        print(f"  {pair:<14} fills={p['n']:<4} volume=${p['notional']:,.2f}  fees=-{p['fees']:.4f}  "
              f"maker={p['maker']}/{p['n']}  fills/day={p['n'] / days:.1f}")

    # ── Open positions on the exchange right now ──────────────────────────────
    live = [p for p in positions if abs(_f(p, "active_pos")) > 0]
    print(f"\nOPEN FUTURES POSITIONS ON EXCHANGE — {len(live)}")
    for p in live:
        print(f"  {p.get('pair')}: qty={_f(p, 'active_pos'):+g} avg={_f(p, 'avg_price'):.2f} "
              f"mark={_f(p, 'mark_price'):.2f} lev={p.get('leverage')} liq={_f(p, 'liquidation_price'):.2f} "
              f"SL={p.get('stop_loss_trigger')} TP={p.get('take_profit_trigger')}")
    print("  (compare with the bot's futures_positions table: any 'open' row not listed here is stale)")

    # ── Spot: round-trip cash flow per market ─────────────────────────────────
    print(f"\nSPOT FILLS — {len(spot)} fills")
    sp = defaultdict(lambda: {"buy": 0.0, "sell": 0.0, "nb": 0, "ns": 0, "fees": 0.0})
    for t in spot:
        p = sp[t.get("market", t.get("symbol", "?"))]
        value = _f(t, "price") * _f(t, "quantity")
        if (t.get("side") or "").lower() == "buy":
            p["buy"] += value; p["nb"] += 1
        else:
            p["sell"] += value; p["ns"] += 1
        p["fees"] += _f(t, "fee_amount")
    for m, p in sorted(sp.items()):
        print(f"  {m:<12} buys={p['nb']} (${p['buy']:.2f})  sells={p['ns']} (${p['sell']:.2f})  "
              f"cash flow={p['sell'] - p['buy']:+.4f}  fees≈{p['fees']:.6f} (fee currency varies)")

    if raw:
        print("\nRAW (first 50 of each)")
        print(json.dumps({"transactions": txns[:50], "futures_fills": fills[:50],
                          "positions": live, "spot_fills": spot[:50]}, indent=1, default=str))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--raw", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.days, a.raw))
