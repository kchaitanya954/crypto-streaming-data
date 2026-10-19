"""
Backtest the ICT / SMT strategy on Binance USD-M futures 1m data.

CoinDCX "B-" futures pairs are priced off Binance, so Binance futures candles are
the closest free history. Monthly/daily zip archives are cached in --data-dir.

Usage:
    python -m backtest.ict_backtest                         # BTC traded, ETH as SMT reference
    python -m backtest.ict_backtest --primary ETHUSDT --reference BTCUSDT
    python -m backtest.ict_backtest --no-smt                # same setup without SMT, for comparison
    python -m backtest.ict_backtest --from 2024-10-01 --to 2026-10-18 --split 2025-10-01
    python -m backtest.ict_backtest --csv trades.csv

Results are reported in R (1R = planned stop distance) net of fees, slippage and
funding, separately for the in-sample and out-of-sample halves.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from signals.ict_smt import Bar, ICTConfig, ICTEngine, NY, ny_time_to_ms  # noqa: E402
from signals.ict_sim import SimCosts, SimTrade, step, summarize  # noqa: E402

_BASE = "https://data.binance.vision/data/futures/um"
TICKS = {"BTCUSDT": 0.1, "ETHUSDT": 0.01}


# ── Data ──────────────────────────────────────────────────────────────────────

def _months(d0: dt.date, d1: dt.date):
    d = d0.replace(day=1)
    while d <= d1:
        yield d
        d = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _fetch(url: str, path: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(path, "wb") as f:
            f.write(r.read())
        return True
    except Exception:
        return False


def ensure_data(symbol: str, d0: dt.date, d1: dt.date, data_dir: str) -> list[str]:
    """Return the archive paths covering [d0, d1], downloading what's missing."""
    os.makedirs(data_dir, exist_ok=True)
    paths = []
    today = dt.date.today()
    for m in _months(d0, d1):
        name = f"{symbol}-1m-{m:%Y-%m}.zip"
        path = os.path.join(data_dir, name)
        month_complete = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1) <= today.replace(day=1)
        if os.path.exists(path) or (month_complete and _fetch(f"{_BASE}/monthly/klines/{symbol}/1m/{name}", path)):
            paths.append(path)
            continue
        # current/unpublished month — fall back to daily archives
        day = max(m, d0)
        while day.month == m.month and day <= d1:
            dname = f"{symbol}-1m-{day:%Y-%m-%d}.zip"
            dpath = os.path.join(data_dir, dname)
            if os.path.exists(dpath) or _fetch(f"{_BASE}/daily/klines/{symbol}/1m/{dname}", dpath):
                paths.append(dpath)
            day += dt.timedelta(days=1)
    return paths


def load_bars(paths: list[str], t0: int, t1: int) -> dict[int, Bar]:
    out: dict[int, Bar] = {}
    for p in paths:
        with zipfile.ZipFile(p) as z:
            for name in z.namelist():
                with z.open(name) as f:
                    for row in csv.reader(io.TextIOWrapper(f)):
                        if not row or not row[0].isdigit():
                            continue          # header line in newer archives
                        t = int(row[0])
                        if t0 <= t < t1:
                            out[t] = Bar(t, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
    return out


# ── Run ───────────────────────────────────────────────────────────────────────

def run(primary: dict[int, Bar], reference: dict[int, Bar], cfg: ICTConfig, tick: float,
        costs: SimCosts, log=None) -> list[SimTrade]:
    eng = ICTEngine(cfg)
    trades: list[SimTrade] = []
    active: SimTrade | None = None
    for t in sorted(primary.keys() & reference.keys()):
        p, r = primary[t], reference[t]
        if active is not None:
            ev = step(active, p, costs)
            if ev == "filled":
                eng.order_finished(True)
            elif ev == "cancelled":
                eng.order_finished(False)
                active = None
            elif ev == "closed":
                if active.fill_ms == active.exit_ms:   # filled and stopped in one bar
                    eng.order_finished(True)
                active = None
        for e in eng.on_minute(p, r):
            if e.kind == "setup" and active is None:
                day = dt.datetime.fromtimestamp(e.t_ms / 1000, dt.timezone.utc).astimezone(NY).date()
                active = SimTrade(e.setup, tick, ny_time_to_ms(day, _hm(cfg.time_stop)))
                trades.append(active)
                if log:
                    log(e)
    return trades


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _fmt(st: dict) -> str:
    if not st.get("trades"):
        return "no trades"
    pf = st["profit_factor"]
    return (f"n={st['trades']:<4} win={st['win_rate'] * 100:5.1f}%  avgW={st['avg_win_r']:+.2f}R  "
            f"avgL={st['avg_loss_r']:+.2f}R  E={st['expectancy_r']:+.3f}R/trade (t={st['t_stat']:+.2f})  "
            f"total={st['total_r']:+.1f}R  PF={'∞' if pf == float('inf') else f'{pf:.2f}'}  "
            f"maxDD={st['max_dd_r']:.1f}R  worst streak={st['worst_losing_streak']}  exits={st['exits']}")


def report(trades: list[SimTrade], costs: SimCosts, split_ms: int) -> None:
    closed = [t for t in trades if t.state == "closed"]
    print(f"setups={len(trades)}  filled={len(closed)}  "
          f"unfilled={sum(1 for t in trades if t.state == 'cancelled')}")
    print("ALL            ", _fmt(summarize(closed, costs)))
    print("IN-SAMPLE      ", _fmt(summarize([t for t in closed if t.setup.created_ms < split_ms], costs)))
    print("OUT-OF-SAMPLE  ", _fmt(summarize([t for t in closed if t.setup.created_ms >= split_ms], costs)))
    print("  long         ", _fmt(summarize([t for t in closed if t.long], costs)))
    print("  short        ", _fmt(summarize([t for t in closed if not t.long], costs)))
    by_lvl = defaultdict(list)
    for t in closed:
        by_lvl[t.setup.smt_level].append(t)
    for k in sorted(by_lvl):
        print(f"  SMT {k:<11}", _fmt(summarize(by_lvl[k], costs)))
    by_q = defaultdict(list)
    for t in closed:
        d = dt.datetime.fromtimestamp(t.setup.created_ms / 1000, dt.timezone.utc)
        by_q[f"{d.year}-Q{(d.month - 1) // 3 + 1}"].append(t)
    for k in sorted(by_q):
        s = summarize(by_q[k], costs)
        print(f"  {k}      n={s['trades']:<3} E={s['expectancy_r']:+.3f}R  total={s['total_r']:+.1f}R")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--primary", default="BTCUSDT")
    ap.add_argument("--reference", default="ETHUSDT")
    ap.add_argument("--from", dest="d0", default="2024-10-01")
    ap.add_argument("--to", dest="d1", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    ap.add_argument("--split", default="2025-10-01", help="out-of-sample starts here")
    ap.add_argument("--data-dir", default="data/klines")
    ap.add_argument("--ltf", type=int, default=5)
    ap.add_argument("--htf", default="60", help="comma list of minutes, e.g. 60,240")
    ap.add_argument("--no-smt", action="store_true")
    ap.add_argument("--entry", default="ce", choices=("ce", "edge"))
    ap.add_argument("--min-rr", type=float, default=2.0)
    ap.add_argument("--max-rr", type=float, default=4.0)
    ap.add_argument("--allow-weekends", action="store_true")
    ap.add_argument("--csv", help="write every setup to this CSV")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    d0, d1 = dt.date.fromisoformat(a.d0), dt.date.fromisoformat(a.d1)
    t0 = int(dt.datetime(d0.year, d0.month, d0.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    t1 = int(dt.datetime(d1.year, d1.month, d1.day, tzinfo=dt.timezone.utc).timestamp() * 1000) + 86_400_000
    s = dt.date.fromisoformat(a.split)
    split_ms = int(dt.datetime(s.year, s.month, s.day, tzinfo=dt.timezone.utc).timestamp() * 1000)

    print(f"Loading {a.primary} / {a.reference} 1m futures candles {d0} → {d1} …", flush=True)
    prim = load_bars(ensure_data(a.primary, d0, d1, a.data_dir), t0, t1)
    ref = load_bars(ensure_data(a.reference, d0, d1, a.data_dir), t0, t1)
    print(f"  {len(prim):,} / {len(ref):,} bars", flush=True)

    cfg = ICTConfig(
        ltf_minutes=a.ltf, htf_minutes=tuple(int(x) for x in a.htf.split(",")),
        require_smt=not a.no_smt, entry_mode=a.entry, min_rr=a.min_rr, max_rr=a.max_rr,
        weekdays_only=not a.allow_weekends,
    )
    costs = SimCosts()
    log = (lambda e: print("  setup", dt.datetime.fromtimestamp(e.t_ms / 1000, NY).strftime("%Y-%m-%d %H:%M NY"),
                           e.setup.direction, f"entry={e.setup.entry:.2f} sl={e.setup.sl:.2f} tp={e.setup.tp:.2f}",
                           "|", "; ".join(e.setup.reasons))) if a.verbose else None
    trades = run(prim, ref, cfg, TICKS.get(a.primary, 0.01), costs, log)
    print(f"\nICT/SMT {a.primary} (ref {a.reference})  ltf={a.ltf}m htf={cfg.htf_minutes} "
          f"smt={cfg.require_smt} entry={cfg.entry_mode} rr={cfg.min_rr}-{cfg.max_rr}  "
          f"split={a.split}  costs: maker {costs.maker * 100:.4f}% taker {costs.taker * 100:.3f}%")
    report(trades, costs, split_ms)

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["created_ny", "direction", "entry", "sl", "tp", "rr", "risk_pct", "smt_level",
                        "smt_swept_by", "tp_source", "state", "exit_reason", "exit_price", "r_net"])
            for t in trades:
                st = t.setup
                w.writerow([dt.datetime.fromtimestamp(st.created_ms / 1000, NY).strftime("%Y-%m-%d %H:%M"),
                            st.direction, f"{st.entry:.2f}", f"{st.sl:.2f}", f"{st.tp:.2f}", f"{st.rr:.2f}",
                            f"{st.risk_pct:.3f}", st.smt_level, st.smt_swept_by, st.tp_source, t.state,
                            t.exit_reason, f"{t.exit_price:.2f}" if t.exit_price else "",
                            f"{t.r_net(costs):+.3f}" if t.state == "closed" else ""])
        print(f"\nwrote {a.csv}")


if __name__ == "__main__":
    main()
