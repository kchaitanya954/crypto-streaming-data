"""
Trade Analysis Script — Last 2 Months
Fetches spot and futures trade history from CoinDCX API, calculates P&L,
and provides algorithm improvement suggestions.

Run: python analyze_trades.py
"""

import asyncio
import hashlib
import hmac
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
import os

load_dotenv()

API_KEY    = os.getenv("COINDCX_API_KEY", "").strip()
API_SECRET = os.getenv("COINDCX_API_SECRET", "").strip()

SPOT_BASE = "https://apigw.coindcx.com"
BASE      = "https://api.coindcx.com"

SPOT_FEE    = 0.001   # 0.1% per side
FUTURES_FEE = 0.0005  # 0.05% per side

TWO_MONTHS_MS = int((time.time() - 60 * 24 * 3600) * 1000)  # 60 days ago in ms


# ── Auth ──────────────────────────────────────────────────────────────────────

def sign(body: dict) -> tuple[str, dict]:
    body_str  = json.dumps(body, separators=(",", ":"))
    signature = hmac.new(
        API_SECRET.encode(),
        body_str.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Content-Type":     "application/json",
        "X-AUTH-APIKEY":    API_KEY,
        "X-AUTH-SIGNATURE": signature,
    }
    return body_str, headers


async def post(session: aiohttp.ClientSession, url: str, body: dict) -> any:
    body_str, headers = sign(body)
    async with session.post(url, data=body_str, headers=headers) as resp:
        text = await resp.text()
        if not resp.ok:
            print(f"  [ERROR] {resp.status} {url}: {text[:300]}")
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print(f"  [ERROR] JSON decode failed for {url}: {text[:200]}")
            return None


# ── Fetch Spot Data ───────────────────────────────────────────────────────────

async def fetch_spot_fills(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch individual fill/execution records (trade_history)."""
    all_fills = []
    from_id = None
    url = f"{SPOT_BASE}/exchange/v1/orders/trade_history"

    while True:
        body: dict = {"timestamp": int(time.time() * 1000), "limit": 500}
        if from_id:
            body["from_id"] = from_id

        result = await post(session, url, body)
        if not result:
            break

        fills = result if isinstance(result, list) else result.get("data", [])
        if not fills:
            break

        # Timestamps: CoinDCX returns epoch ms in "timestamp" or "created_at"
        batch = []
        for f in fills:
            ts = f.get("timestamp") or f.get("created_at") or 0
            if isinstance(ts, str):
                # Some endpoints return ISO string
                try:
                    ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
                except Exception:
                    ts = 0
            f["_ts_ms"] = int(ts)
            batch.append(f)

        # Stop paginating when we reach beyond our window
        oldest = min(f["_ts_ms"] for f in batch) if batch else 0
        all_fills.extend([f for f in batch if f["_ts_ms"] >= TWO_MONTHS_MS])

        if oldest < TWO_MONTHS_MS or len(fills) < 500:
            break

        # Paginate using the last trade_id or id
        last = fills[-1]
        from_id = last.get("trade_id") or last.get("id")
        if not from_id:
            break
        await asyncio.sleep(0.3)

    return all_fills


async def fetch_spot_orders(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch historical orders (filled/cancelled/closed)."""
    all_orders = []
    url = f"{SPOT_BASE}/exchange/v1/orders"

    body: dict = {"timestamp": int(time.time() * 1000), "limit": 500}
    result = await post(session, url, body)
    if not result:
        return []

    orders = result if isinstance(result, list) else result.get("data", [])
    for o in orders:
        ts = o.get("created_at") or o.get("timestamp") or 0
        if isinstance(ts, str):
            try:
                ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts = 0
        o["_ts_ms"] = int(ts)
        if o["_ts_ms"] >= TWO_MONTHS_MS:
            all_orders.append(o)

    return all_orders


# ── Fetch Futures Data ────────────────────────────────────────────────────────

async def fetch_futures_positions(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch all futures positions (open and closed)."""
    url = f"{BASE}/exchange/v1/derivatives/futures/positions"
    body: dict = {"timestamp": int(time.time() * 1000)}
    result = await post(session, url, body)
    if not result:
        return []
    positions = result if isinstance(result, list) else result.get("positions", [])

    filtered = []
    for p in positions:
        ts = p.get("created_at") or p.get("timestamp") or 0
        if isinstance(ts, str):
            try:
                ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts = 0
        p["_ts_ms"] = int(ts)
        if p["_ts_ms"] >= TWO_MONTHS_MS:
            filtered.append(p)

    return filtered


async def fetch_futures_orders(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch futures order history."""
    url = f"{BASE}/exchange/v1/derivatives/futures/orders"
    body: dict = {"timestamp": int(time.time() * 1000), "limit": 500}
    result = await post(session, url, body)
    if not result:
        return []
    orders = result if isinstance(result, list) else result.get("data", [])

    filtered = []
    for o in orders:
        ts = o.get("created_at") or o.get("timestamp") or 0
        if isinstance(ts, str):
            try:
                ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                ts = 0
        o["_ts_ms"] = int(ts)
        if o["_ts_ms"] >= TWO_MONTHS_MS:
            filtered.append(o)

    return filtered


# ── P&L Calculation ───────────────────────────────────────────────────────────

def pair_fills(fills: list[dict]) -> dict[str, list[dict]]:
    """Group fills by market symbol."""
    by_market: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        market = f.get("market") or f.get("symbol") or "UNKNOWN"
        by_market[market].append(f)
    for market in by_market:
        by_market[market].sort(key=lambda x: x["_ts_ms"])
    return dict(by_market)


def calculate_pnl(fills: list[dict]) -> dict:
    """FIFO-match BUY and SELL fills to compute trade cycles and P&L."""
    buy_queue: list[dict] = []
    cycles: list[dict] = []
    open_buys: list[dict] = []

    for f in fills:
        side = (f.get("side") or "").lower()
        qty  = float(f.get("quantity") or f.get("filled_quantity") or 0)
        price = float(f.get("price") or f.get("avg_price") or f.get("filled_price") or 0)
        fee   = float(f.get("fee_amount") or 0)
        ts    = f["_ts_ms"]

        if qty == 0 or price == 0:
            continue

        if side == "buy":
            buy_queue.append({"qty": qty, "price": price, "fee": fee, "ts": ts, "raw": f})
        elif side == "sell":
            remaining_sell_qty = qty
            sell_fee = fee

            while remaining_sell_qty > 1e-9 and buy_queue:
                buy = buy_queue[0]
                matched_qty = min(remaining_sell_qty, buy["qty"])

                buy_cost   = buy["price"] * matched_qty
                sell_proc  = price * matched_qty
                # Apportion fees proportionally
                buy_fee_part  = buy["fee"] * (matched_qty / (buy["qty"] + matched_qty))
                sell_fee_part = sell_fee * (matched_qty / qty) if qty > 0 else 0

                gross_pnl_usdt = sell_proc - buy_cost
                net_pnl_usdt   = gross_pnl_usdt - buy_fee_part - sell_fee_part
                gross_pnl_pct  = (price - buy["price"]) / buy["price"] * 100
                net_pnl_pct    = gross_pnl_pct - (SPOT_FEE * 2 * 100)

                cycles.append({
                    "market":         f.get("market") or f.get("symbol"),
                    "buy_price":      buy["price"],
                    "sell_price":     price,
                    "qty":            matched_qty,
                    "gross_pnl_usdt": gross_pnl_usdt,
                    "net_pnl_usdt":   net_pnl_usdt,
                    "gross_pnl_pct":  gross_pnl_pct,
                    "net_pnl_pct":    net_pnl_pct,
                    "fees_usdt":      buy_fee_part + sell_fee_part,
                    "buy_ts":         buy["ts"],
                    "sell_ts":        ts,
                    "hold_ms":        ts - buy["ts"],
                })

                buy["qty"] -= matched_qty
                remaining_sell_qty -= matched_qty

                if buy["qty"] < 1e-9:
                    buy_queue.pop(0)

    open_buys = [b for b in buy_queue if b["qty"] > 1e-9]

    return {"cycles": cycles, "open_buys": open_buys}


def fmt_ts(ms: int) -> str:
    if not ms:
        return "N/A"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(ms)


def fmt_hold(ms: int) -> str:
    if ms <= 0:
        return "N/A"
    mins = ms // 60000
    if mins < 60:
        return f"{mins}m"
    hrs = mins // 60
    rem = mins % 60
    if hrs < 24:
        return f"{hrs}h {rem}m"
    days = hrs // 24
    return f"{days}d {hrs % 24}h"


# ── Algorithm Improvement Analysis ───────────────────────────────────────────

def generate_improvements(all_cycles: list[dict], open_buys_count: int) -> list[str]:
    suggestions = []
    n = len(all_cycles)

    if n == 0:
        suggestions.append("No completed trade cycles found — cannot generate data-driven improvements.")
        suggestions.append("Check that the bot has been executing trades and that CoinDCX API keys are correct.")
        return suggestions

    wins      = [c for c in all_cycles if c["net_pnl_pct"] > 0]
    losses    = [c for c in all_cycles if c["net_pnl_pct"] <= 0]
    win_rate  = len(wins) / n * 100
    avg_win   = sum(c["net_pnl_pct"] for c in wins) / len(wins) if wins else 0
    avg_loss  = sum(c["net_pnl_pct"] for c in losses) / len(losses) if losses else 0

    total_net  = sum(c["net_pnl_usdt"] for c in all_cycles)
    total_fees = sum(c["fees_usdt"] for c in all_cycles)

    # Fee drag check — trades that are within ±0.25% (fee zone)
    fee_zone = [c for c in all_cycles if abs(c["gross_pnl_pct"]) < 0.25]
    fee_zone_pct = len(fee_zone) / n * 100

    # Hold time analysis
    hold_times = [c["hold_ms"] for c in all_cycles if c["hold_ms"] > 0]
    avg_hold_min = (sum(hold_times) / len(hold_times) / 60000) if hold_times else 0

    # Tight SL check: how many losses are in -1% to -3% range (SL zone)
    sl_range = [c for c in losses if -3.5 <= c["net_pnl_pct"] <= -0.5]
    sl_pct   = len(sl_range) / n * 100 if n > 0 else 0

    print()
    print("=" * 65)
    print(" ALGORITHM IMPROVEMENT ANALYSIS")
    print("=" * 65)

    rank = 1

    # 1. Win rate
    if win_rate < 40:
        suggestions.append(
            f"[#{rank}] CRITICAL — Win rate is {win_rate:.1f}% (below 40%). Your entry signals "
            f"have too many false positives. Fixes:\n"
            f"   • Raise ADX minimum to 25–30 (currently ~20) to filter choppy markets\n"
            f"   • Require 15m EMA alignment before taking a 1m signal\n"
            f"   • Only trade HIGH confidence signals — drop MEDIUM/LOW\n"
            f"   • Add volume spike confirmation: OBV rising >5% on signal bar"
        )
        rank += 1
    elif win_rate < 55:
        suggestions.append(
            f"[#{rank}] Win rate {win_rate:.1f}% — marginally below 55% break-even zone (with "
            f"your 4:2 TP:SL ratio). Tighten entry: raise ADX to 23+, require 2+ confirmations."
        )
        rank += 1

    # 2. Fee drag
    if fee_zone_pct > 30:
        suggestions.append(
            f"[#{rank}] HIGH IMPACT — {fee_zone_pct:.0f}% of trades have gross move < 0.25% "
            f"(inside fee zone). You're paying 0.2% round-trip fees on tiny moves and losing money. "
            f"Fixes:\n"
            f"   • Increase minimum TP from 4% to 6%+ or use trailing SL to let winners run\n"
            f"   • Switch from 1m to 5m or 15m intervals — fewer signals, larger moves\n"
            f"   • Add a minimum-move filter: only trade when ATR(14) > 0.3% of price"
        )
        rank += 1

    # 3. Average hold time
    if avg_hold_min < 5:
        suggestions.append(
            f"[#{rank}] SCALPING TRAP — Average hold time is {avg_hold_min:.1f} minutes. "
            f"At 0.2% round-trip fees, you need the price to move >0.2% in {avg_hold_min:.0f} "
            f"minutes just to break even. Most coins don't move that fast on 1m bars. Fix:\n"
            f"   • Move to 5m or 15m candles — same MACD logic but larger, real moves\n"
            f"   • Add a cooldown of 10+ bars after each trade (already in detector, may need raising)"
        )
        rank += 1

    # 4. Risk:reward check
    if wins and losses and abs(avg_loss) > avg_win * 0.8:
        rr = avg_win / abs(avg_loss) if avg_loss != 0 else 0
        suggestions.append(
            f"[#{rank}] POOR RISK:REWARD — Average win {avg_win:.2f}% vs average loss "
            f"{avg_loss:.2f}% (ratio: {rr:.2f}:1, need >1.5:1). Fixes:\n"
            f"   • Implement trailing SL: once trade is +2%, trail SL at +0.5% so "
            f"     you never give back all gains\n"
            f"   • Raise TP to 6% (currently 4%) — gives more room to run\n"
            f"   • Move SL from fixed 2% to ATR-based: use 1.5×ATR(14) so SL is "
            f"     market-adaptive, not arbitrary"
        )
        rank += 1

    # 5. SL cluster
    if sl_pct > 25:
        suggestions.append(
            f"[#{rank}] SL WHIPSAW — {sl_pct:.0f}% of trades hit stop-loss in the -0.5% to "
            f"-3.5% range. Crypto has high short-term volatility; your fixed 2% SL is triggering "
            f"on normal noise. Fixes:\n"
            f"   • Use ATR-based SL: sl_price = entry - max(1.5 × ATR14, 0.5% of price)\n"
            f"   • Add 15-minute confirmation before setting SL — allow initial volatility\n"
            f"   • Widen SL to 3.5% for BTC/ETH which have higher ATR naturally"
        )
        rank += 1

    # 6. Structural improvements (always include)
    suggestions.append(
        f"[#{rank}] STRUCTURE — Add 15m multi-timeframe (MTF) filter:\n"
        f"   • Only take BUY signals when 15m EMA(20) > EMA(50) (uptrend context)\n"
        f"   • Only take SELL signals when 15m EMA(20) < EMA(50)\n"
        f"   • This alone typically raises win rate 8–15% by avoiding counter-trend scalps\n"
        f"   File to modify: signals/detector.py — add mtf_check() before signal emit"
    )
    rank += 1

    suggestions.append(
        f"[#{rank}] TIMING — Avoid low-liquidity hours (12 AM–6 AM IST / 18:30–00:30 UTC):\n"
        f"   • Volume is thin, spreads are wide, MACD signals are noisy\n"
        f"   • Add a time filter in orchestrator.py signal worker loop\n"
        f"   • Expected impact: 10–20% fewer trades but higher quality entry"
    )
    rank += 1

    if total_fees > abs(total_net) * 0.5:
        suggestions.append(
            f"[#{rank}] FEES — Total fees paid ({total_fees:.4f} USDT) are more than 50% of "
            f"your gross P&L. Consider CoinDCX's maker orders (limit orders) which charge "
            f"0.05% instead of 0.1% taker fee. This halves your round-trip cost to 0.1%."
        )
        rank += 1

    suggestions.append(
        f"[#{rank}] ADAPTIVE SIZING — The Half-Kelly engine is correct in principle but needs "
        f"more trade history to calibrate. With <30 trades, Kelly is unreliable. Temporary fix:\n"
        f"   • Hard-cap trade size at 3% of total USDT balance (not budget slice)\n"
        f"   • Enable Kelly auto-scaling only after 50+ closed cycles\n"
        f"   File: signals/adaptive_strategy.py — add min_trades guard before kelly_fraction()"
    )

    return suggestions


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: COINDCX_API_KEY / COINDCX_API_SECRET not set in .env")
        return

    since_dt = datetime.fromtimestamp(TWO_MONTHS_MS / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    now_dt   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    print()
    print("=" * 65)
    print(f" CRYPTO TRADE ANALYSIS  ({since_dt}  →  {now_dt})")
    print("=" * 65)
    print(f" API Key: {API_KEY[:8]}...{API_KEY[-4:]}")
    print()

    async with aiohttp.ClientSession() as session:

        # ── SPOT ─────────────────────────────────────────────────────────────
        print("Fetching spot trade fills ...")
        fills = await fetch_spot_fills(session)

        print("Fetching spot order history ...")
        orders = await fetch_spot_orders(session)

        print(f"  Fills in window:  {len(fills)}")
        print(f"  Orders in window: {len(orders)}")

        # ── FUTURES ──────────────────────────────────────────────────────────
        print()
        print("Fetching futures positions ...")
        fut_positions = await fetch_futures_positions(session)

        print("Fetching futures orders ...")
        fut_orders = await fetch_futures_orders(session)

        print(f"  Futures positions in window: {len(fut_positions)}")
        print(f"  Futures orders in window:    {len(fut_orders)}")

    # ── Process Spot ──────────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print(f" SPOT TRADES  ({since_dt} – {now_dt})")
    print("=" * 65)

    all_cycles: list[dict] = []
    all_open_buys: list[dict] = []

    if not fills:
        print("  No spot fills found in the last 2 months.")
    else:
        by_market = pair_fills(fills)

        for market, mfills in sorted(by_market.items()):
            result     = calculate_pnl(mfills)
            cycles     = result["cycles"]
            open_buys  = result["open_buys"]
            all_cycles.extend(cycles)
            all_open_buys.extend(open_buys)

            n_cycles  = len(cycles)
            n_buys    = sum(1 for f in mfills if (f.get("side") or "").lower() == "buy")
            n_sells   = sum(1 for f in mfills if (f.get("side") or "").lower() == "sell")

            if n_cycles == 0 and not open_buys:
                print(f"\n  {market}: {n_buys} buys, {n_sells} sells — no completed cycles")
                continue

            net_pnl = sum(c["net_pnl_usdt"] for c in cycles)
            fees    = sum(c["fees_usdt"] for c in cycles)
            wins    = [c for c in cycles if c["net_pnl_pct"] > 0]
            losses  = [c for c in cycles if c["net_pnl_pct"] <= 0]

            print(f"\n  {'─'*55}")
            print(f"  {market}")
            print(f"  {'─'*55}")
            print(f"  Fills — buys: {n_buys}, sells: {n_sells}")
            print(f"  Completed cycles: {n_cycles}  |  Open buys: {len(open_buys)}")
            print(f"  Profitable: {len(wins)}  |  Loss: {len(losses)}")
            if n_cycles:
                wr = len(wins) / n_cycles * 100
                avg_w = sum(c["net_pnl_pct"] for c in wins) / len(wins) if wins else 0
                avg_l = sum(c["net_pnl_pct"] for c in losses) / len(losses) if losses else 0
                print(f"  Win rate: {wr:.1f}%  |  Avg win: {avg_w:+.3f}%  |  Avg loss: {avg_l:+.3f}%")
                print(f"  Total fees: {fees:.4f} USDT  |  Net P&L: {net_pnl:+.4f} USDT")

                print(f"\n  Trade Log:")
                for i, c in enumerate(cycles, 1):
                    sign_str = "WIN " if c["net_pnl_pct"] > 0 else "LOSS"
                    hold_str = fmt_hold(c["hold_ms"])
                    print(
                        f"    #{i:02d} [{sign_str}] "
                        f"buy@{c['buy_price']:.4f} → sell@{c['sell_price']:.4f}  "
                        f"qty={c['qty']:.6f}  "
                        f"net={c['net_pnl_pct']:+.3f}%  "
                        f"({c['net_pnl_usdt']:+.4f} USDT)  "
                        f"held={hold_str}  "
                        f"[{fmt_ts(c['sell_ts'])}]"
                    )

        # Summary
        print()
        print("=" * 65)
        print(" SPOT SUMMARY")
        print("=" * 65)

        total_cycles = len(all_cycles)
        if total_cycles > 0:
            total_net   = sum(c["net_pnl_usdt"] for c in all_cycles)
            total_fees  = sum(c["fees_usdt"] for c in all_cycles)
            total_gross = sum(c["gross_pnl_usdt"] for c in all_cycles)
            all_wins    = [c for c in all_cycles if c["net_pnl_pct"] > 0]
            all_losses  = [c for c in all_cycles if c["net_pnl_pct"] <= 0]
            win_rate    = len(all_wins) / total_cycles * 100

            best  = max(all_cycles, key=lambda x: x["net_pnl_pct"])
            worst = min(all_cycles, key=lambda x: x["net_pnl_pct"])

            hold_times   = [c["hold_ms"] for c in all_cycles if c["hold_ms"] > 0]
            avg_hold_min = (sum(hold_times) / len(hold_times) / 60000) if hold_times else 0

            print(f"  Total completed cycles:  {total_cycles}")
            print(f"  Open (unpaired buys):    {len(all_open_buys)}")
            print(f"  Profitable trades:       {len(all_wins)}")
            print(f"  Loss-making trades:      {len(all_losses)}")
            print(f"  Win rate:               {win_rate:.1f}%")
            print()
            print(f"  Gross P&L:   {total_gross:+.4f} USDT")
            print(f"  Total fees:  -{total_fees:.4f} USDT")
            print(f"  Net P&L:     {total_net:+.4f} USDT")
            print()
            print(f"  Best trade:  {best['market']}  {best['net_pnl_pct']:+.3f}%  ({fmt_ts(best['sell_ts'])})")
            print(f"  Worst trade: {worst['market']}  {worst['net_pnl_pct']:+.3f}%  ({fmt_ts(worst['sell_ts'])})")
            print(f"  Avg hold:    {avg_hold_min:.1f} min")

            verdict = "PROFITABLE" if total_net > 0 else "IN LOSS"
            print()
            print(f"  >>> VERDICT: {verdict} ({total_net:+.4f} USDT net over 60 days)")
        else:
            print("  No completed spot cycles in the last 2 months.")

    # ── Process Futures ───────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print(f" FUTURES TRADES  ({since_dt} – {now_dt})")
    print("=" * 65)

    fut_cycles: list[dict] = []

    if not fut_positions and not fut_orders:
        print("  No futures activity found in the last 2 months.")
    else:
        if fut_positions:
            print(f"  Positions found: {len(fut_positions)}")
            for p in fut_positions:
                status     = p.get("status", "?")
                pair       = p.get("pair") or p.get("symbol") or "?"
                side       = p.get("side", "?")
                entry      = float(p.get("entry_price") or p.get("avg_price") or 0)
                lev        = p.get("leverage", "?")
                pnl_pct    = float(p.get("pnl_pct") or p.get("unrealized_pnl_pct") or 0)
                pnl_usdt   = float(p.get("pnl_usdt") or p.get("realized_pnl") or p.get("pnl") or 0)
                margin      = float(p.get("margin_usdt") or p.get("margin") or 0)
                ts_str      = fmt_ts(p["_ts_ms"])

                print(f"\n  {pair} [{side.upper()}] status={status} leverage={lev}x")
                print(f"    Entry: {entry}  |  Margin: {margin:.2f} USDT")
                print(f"    P&L: {pnl_pct:+.3f}%  ({pnl_usdt:+.4f} USDT)")
                print(f"    Opened: {ts_str}")

                if status in ("closed", "settled"):
                    fut_cycles.append({
                        "pair": pair, "side": side, "pnl_pct": pnl_pct,
                        "pnl_usdt": pnl_usdt, "margin": margin, "ts": p["_ts_ms"]
                    })

        if fut_orders:
            print(f"\n  Order count: {len(fut_orders)}")
            for o in fut_orders[:20]:  # cap display at 20
                side   = o.get("side", "?")
                pair   = o.get("pair") or o.get("symbol") or "?"
                status = o.get("status", "?")
                qty    = o.get("total_quantity") or o.get("quantity") or "?"
                price  = o.get("avg_price") or o.get("price") or "?"
                ts_str = fmt_ts(o["_ts_ms"])
                print(f"    {ts_str}  {pair} {side.upper()} qty={qty}  price={price}  status={status}")

        if fut_cycles:
            fut_net = sum(c["pnl_usdt"] for c in fut_cycles)
            fut_wins = [c for c in fut_cycles if c["pnl_pct"] > 0]
            print()
            print(f"  Futures closed positions: {len(fut_cycles)}")
            print(f"  Win: {len(fut_wins)}  |  Loss: {len(fut_cycles) - len(fut_wins)}")
            print(f"  Net Futures P&L: {fut_net:+.4f} USDT")
        else:
            print("\n  No closed futures positions in window (may be open or never traded).")

    # ── CoinDCX Verification Summary ──────────────────────────────────────────
    print()
    print("=" * 65)
    print(" COINDCX API VERIFICATION")
    print("=" * 65)
    print(f"  Spot fills fetched from API:     {len(fills)}")
    print(f"  Spot orders fetched from API:    {len(orders)}")
    print(f"  Futures positions from API:      {len(fut_positions)}")
    print(f"  Futures orders from API:         {len(fut_orders)}")
    print()

    # Cross-reference fills with orders
    fill_order_ids = {f.get("order_id") for f in fills if f.get("order_id")}
    order_ids      = {o.get("id") for o in orders if o.get("id")}
    fills_not_in_orders = fill_order_ids - order_ids
    orders_not_filled   = {
        o.get("id") for o in orders
        if o.get("status") == "filled" and o.get("id") not in fill_order_ids
    }

    print(f"  Fill order_ids matched in orders: {len(fill_order_ids & order_ids)}/{len(fill_order_ids)}")
    if fills_not_in_orders:
        print(f"  Fills with no matching order: {list(fills_not_in_orders)[:5]}")
    if orders_not_filled:
        print(f"  Filled orders with no fill record: {list(orders_not_filled)[:5]}")

    if not fills_not_in_orders and not orders_not_filled:
        print("  All records cross-verified — data is consistent.")

    # ── Algorithm Improvements ────────────────────────────────────────────────
    suggestions = generate_improvements(all_cycles, len(all_open_buys))
    for s in suggestions:
        print()
        print(f"  {s}")

    print()
    print("=" * 65)
    print(" ANALYSIS COMPLETE")
    print("=" * 65)
    print()


if __name__ == "__main__":
    asyncio.run(main())
