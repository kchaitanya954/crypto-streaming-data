"""
Backtest an indicator trigger (signals.detector) the way the bot actually trades it.

Uses the live modules — SignalDetector + params_for_interval, compute_futures_sl_tp,
the spot SL/TP/trailing constants and the futures trailing/breakeven monitor — so the
result describes the code that runs, not an idealised version of it.

  spot     long only; buy on BUY, sell on any SELL; 3% SL / 6% TP / trailing stop,
           all checked on the candle close (as orchestrator._check_sl_tp does).
           Fee 0.2% per side.
  futures  BUY → long, SELL → short (unless --long-only); exit signals only close;
           exchange-side ATR SL/TP hit intrabar (stop first if both); breakeven and
           trailing stop enforced on the close. Taker 0.059% + 2 bps stop slippage,
           funding 0.01%/8h.

Usage:
    python -m backtest.trigger_backtest --symbol BTCUSDT --interval 1d
    python -m backtest.trigger_backtest --symbol ETHUSDT --interval 4h --market futures --long-only

Returns are per trade on notional (leverage scales them, it doesn't create them).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange import auto_trade, futures_monitor as fm  # noqa: E402
from exchange.fees import FUTURES_TAKER_FEE_RATE, SPOT_FEE_RATE  # noqa: E402
from exchange.futures_trade import compute_futures_sl_tp  # noqa: E402
from indicators.indicators import atr as calc_atr  # noqa: E402
from signals.detector import SignalDetector, params_for_interval  # noqa: E402
from streaming.stream import Kline, _fetch_futures_klines_range_sync  # noqa: E402

WINDOW = 400          # indicator history kept per bar (live seeds 201 bars)
STOP_SLIP = 0.0002
FUNDING_8H = 0.0001


def _ms_per_bar(interval: str) -> int:
    n, u = int(interval[:-1]), interval[-1]
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[u]


def signals_for(bars: list[Kline], interval: str) -> list:
    """The detector's signal (or None) after each closed bar, with bounded history."""
    det = SignalDetector(**params_for_interval(interval))
    out = []
    for k in bars:
        out.append(det.update(k))
        if len(det.closes) > WINDOW:
            for name in ("opens", "closes", "highs", "lows", "volumes", "open_times"):
                del getattr(det, name)[:-WINDOW]
    return out


def _atr_pct(bars, i):
    seg = bars[max(0, i - 29): i + 1]
    a = calc_atr([b.high for b in seg], [b.low for b in seg], [b.close for b in seg], period=14)
    v = next((x for x in reversed(a) if x is not None), None)
    return v / bars[i].close * 100 if v else 0.8


def sim_spot(bars, sigs):
    fee = SPOT_FEE_RATE
    trades: list[dict] = []
    pos: dict = {}                                # empty = flat
    for i, (k, s) in enumerate(zip(bars, sigs)):
        if pos:                                   # orchestrator._check_sl_tp, on the close
            if k.close >= pos["eff"] * (1 + auto_trade.TRAILING_ACTIVATION_PCT / 100):
                pos["sl"] = max(pos["sl"], k.close * (1 - auto_trade.TRAILING_DISTANCE_PCT / 100))
            if k.close <= pos["sl"] or k.close >= pos["tp"]:
                trades.append(_close_spot(pos, k, "sl" if k.close <= pos["sl"] else "tp", fee))
                pos = {}
                continue
        if s is None:
            continue
        if s.direction == "SELL" and pos:
            trades.append(_close_spot(pos, k, "signal", fee))
            pos = {}
        elif s.direction == "BUY" and not pos and not s.is_exit:
            eff = k.close * (1 + fee)
            pos = {"t": k.open_time, "entry": k.close, "eff": eff,
                   "sl": eff * (1 - auto_trade.STOP_LOSS_PCT / 100),
                   "tp": eff * (1 + auto_trade.TAKE_PROFIT_PCT / 100 + fee)}
    return trades


def _close_spot(pos, k, why, fee):
    ret = k.close * (1 - fee) / pos["eff"] - 1
    return {"t": pos["t"], "exit_t": k.open_time, "side": "long", "ret": ret, "why": why}


def sim_futures(bars, sigs, interval, leverage=5, long_only=False):
    fee, trades = FUTURES_TAKER_FEE_RATE, []
    pos: dict = {}                                # empty = flat
    per_bar_8h = _ms_per_bar(interval) / 28_800_000
    for i, (k, s) in enumerate(zip(bars, sigs)):
        if pos and i > pos["i"]:
            long_ = pos["side"] == "long"
            pos["funding"] += FUNDING_8H * per_bar_8h * (1 if long_ else -1)
            # exchange-side SL/TP during the bar (stop first when both are inside the range)
            hit_sl = k.low <= pos["sl_x"] if long_ else k.high >= pos["sl_x"]
            hit_tp = k.high >= pos["tp"] if long_ else k.low <= pos["tp"]
            if hit_sl or hit_tp:
                px = pos["sl_x"] * ((1 - STOP_SLIP) if long_ else (1 + STOP_SLIP)) if hit_sl else pos["tp"]
                trades.append(_close_fut(pos, px, k, "sl" if hit_sl else "tp", fee))
                pos = {}
            else:
                # software SL from the trailing/breakeven monitor, checked on the close
                if (long_ and k.close <= pos["sl_sw"]) or (not long_ and k.close >= pos["sl_sw"]):
                    trades.append(_close_fut(pos, k.close, k, "trail", fee))
                    pos = {}
                else:
                    _monitor(pos, k.close)
        if s is None:
            continue
        want = "long" if s.direction == "BUY" else "short"
        if pos and pos["side"] != want:
            trades.append(_close_fut(pos, k.close, k, "signal", fee))
            pos = {}
        if s.is_exit or pos or (want == "short" and long_only):
            continue
        sl, tp, _ = compute_futures_sl_tp(want, k.close, leverage, _atr_pct(bars, i), price_tick=0.01)
        pos = {"i": i, "t": k.open_time, "side": want, "entry": k.close, "sl_x": sl, "sl_sw": sl,
               "tp": tp, "funding": 0.0}
    return trades


def _monitor(pos, price):
    entry, sl, tp = pos["entry"], pos["sl_sw"], pos["tp"]
    if pos["side"] == "long":
        new = sl
        if tp - entry > 0 and price >= entry + fm.BREAKEVEN_TRIGGER * (tp - entry):
            new = max(new, entry * (1 + fm.BREAKEVEN_BUFFER))
        if (price - entry) / entry >= fm.TRAIL_ACTIVATION_PCT:
            new = max(new, price * (1 - fm.TRAIL_PCT))
        pos["sl_sw"] = new
    else:
        new = sl
        if entry - tp > 0 and price <= entry - fm.BREAKEVEN_TRIGGER * (entry - tp):
            new = min(new, entry * (1 - fm.BREAKEVEN_BUFFER))
        if (entry - price) / entry >= fm.TRAIL_ACTIVATION_PCT:
            new = min(new, price * (1 + fm.TRAIL_PCT))
        pos["sl_sw"] = new


def _close_fut(pos, px, k, why, fee):
    gross = (px / pos["entry"] - 1) if pos["side"] == "long" else (1 - px / pos["entry"])
    return {"t": pos["t"], "exit_t": k.open_time, "side": pos["side"],
            "ret": gross - fee * (1 + px / pos["entry"]) - pos["funding"], "why": why}


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rs = [t["ret"] for t in trades]
    w, l = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    m = sum(rs) / len(rs)
    sd = (sum((r - m) ** 2 for r in rs) / max(1, len(rs) - 1)) ** 0.5
    return {"n": len(rs), "win": len(w) / len(rs), "avg": m, "t": m / (sd / len(rs) ** 0.5) if sd else 0,
            "sum": sum(rs), "pf": sum(w) / -sum(l) if l and sum(l) < 0 else float("inf"), "dd": dd}


def fmt(s: dict) -> str:
    if not s.get("n"):
        return "no trades"
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"n={s['n']:<4} win={s['win'] * 100:4.0f}%  avg={s['avg'] * 100:+.2f}%/trade (t={s['t']:+.2f})  "
            f"sum={s['sum'] * 100:+7.1f}%  PF={pf:<5} maxDD={s['dd'] * 100:6.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--market", choices=("spot", "futures", "both"), default="both")
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--from", dest="d0", default="2019-09-01")
    ap.add_argument("--split", default="2023-01-01", help="out-of-sample starts here")
    a = ap.parse_args()

    t0 = int(dt.datetime.fromisoformat(a.d0).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    split = int(dt.datetime.fromisoformat(a.split).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    bars = _fetch_futures_klines_range_sync(a.symbol, a.interval, t0, now)
    run(a.symbol, a.interval, bars, split, a.market, a.long_only, a.leverage)


def run(symbol, interval, bars, split, market="both", long_only=False, leverage=5) -> dict:
    sigs = signals_for(bars, interval)
    first = next(i for i, b in enumerate(bars) if b.open_time >= split)
    bh = bars[-1].close / bars[first].close - 1
    print(f"{symbol} {interval}: {len(bars)} bars, {sum(1 for s in sigs if s)} signals · "
          f"buy&hold over OOS {bh * 100:+.0f}%")
    out = {}
    runs = []
    if market in ("spot", "both"):
        runs.append(("spot", sim_spot(bars, sigs)))
    if market in ("futures", "both"):
        runs.append(("futures L/S", sim_futures(bars, sigs, interval, leverage, long_only=False)))
        runs.append(("futures long", sim_futures(bars, sigs, interval, leverage, long_only=True)))
    for name, tr in runs:
        ins, oos = [t for t in tr if t["t"] < split], [t for t in tr if t["t"] >= split]
        out[name] = {"is": stats(ins), "oos": stats(oos)}
        print(f"  {name:<13} IS  {fmt(out[name]['is'])}")
        print(f"  {'':<13} OOS {fmt(out[name]['oos'])}")
    return out


if __name__ == "__main__":
    main()
