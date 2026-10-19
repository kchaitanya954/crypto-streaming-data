"""
Order/position simulator for ICT setups — shared by the backtester and paper mode.

Deliberately conservative so a backtest can't flatter the strategy:
  - a resting limit fills only if price trades THROUGH it by one tick (a touch is
    not a fill — you'd be at the back of the queue)
  - if a candle reaches both the stop and the target, the stop wins
  - no target fill on the candle that filled the entry
  - CoinDCX fees as charged (incl. 18% GST): maker 0.0236% entry, taker 0.059% exit
    (exchange TP/SL orders are market orders when triggered)
  - stop slippage 2 bps, target/time-exit slippage 1 bp
  - funding at 00/08/16 UTC while open: longs pay, shorts receive (approximate)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from signals.ict_smt import Bar, Setup

FUNDING_HOURS_UTC = (0, 8, 16)


@dataclass
class SimCosts:
    maker:      float = 0.000236
    taker:      float = 0.00059
    stop_slip:  float = 0.0002
    tp_slip:    float = 0.0001
    exit_slip:  float = 0.0001
    funding_8h: float = 0.0001


@dataclass
class SimTrade:
    setup:        Setup
    tick:         float
    time_stop_ms: int
    state:        str = "pending"          # pending → open → closed | cancelled
    fill_ms:      Optional[int] = None
    exit_ms:      Optional[int] = None
    exit_price:   Optional[float] = None
    exit_reason:  str = ""
    funding:      float = 0.0              # per unit of base asset, + = cost

    @property
    def long(self) -> bool:
        return self.setup.direction == "long"

    def pnl_per_unit(self, costs: SimCosts) -> float:
        s = self.setup
        if self.exit_price is None:
            return 0.0
        gross = (self.exit_price - s.entry) if self.long else (s.entry - self.exit_price)
        fees = s.entry * costs.maker + self.exit_price * costs.taker
        return gross - fees - self.funding

    def r_net(self, costs: SimCosts) -> float:
        risk = abs(self.setup.entry - self.setup.sl)
        return self.pnl_per_unit(costs) / risk if risk else 0.0


def step(tr: SimTrade, bar: Bar, costs: SimCosts) -> Optional[str]:
    """
    Advance one trade by one closed 1m bar. Returns "filled", "cancelled" or
    "closed" when the state changes, else None.
    """
    s = tr.setup
    if tr.state == "pending":
        if bar.t >= s.expiry_ms:
            tr.state, tr.exit_reason = "cancelled", "expired unfilled"
            return "cancelled"
        through = (bar.l <= s.entry - tr.tick) if tr.long else (bar.h >= s.entry + tr.tick)
        if not through:
            reached_tp = bar.h >= s.tp if tr.long else bar.l <= s.tp
            if reached_tp:
                tr.state, tr.exit_reason = "cancelled", "target reached before the retrace"
                return "cancelled"
            return None
        tr.state, tr.fill_ms = "open", bar.t
        hit_sl = bar.l <= s.sl if tr.long else bar.h >= s.sl
        if hit_sl:
            _close(tr, bar.t, s.sl * (1 - costs.stop_slip) if tr.long else s.sl * (1 + costs.stop_slip), "stop loss")
            return "closed"
        return "filled"

    if tr.state != "open":
        return None

    if bar.t // 3_600_000 * 3_600_000 == bar.t and (bar.t // 3_600_000) % 24 in FUNDING_HOURS_UTC:
        f = s.entry * costs.funding_8h
        tr.funding += f if tr.long else -f

    if tr.long:
        if bar.l <= s.sl:
            _close(tr, bar.t, s.sl * (1 - costs.stop_slip), "stop loss")
        elif bar.h >= s.tp:
            _close(tr, bar.t, s.tp * (1 - costs.tp_slip), "take profit")
        elif bar.t + 60000 >= tr.time_stop_ms:
            _close(tr, bar.t, bar.c * (1 - costs.exit_slip), "time stop")
    else:
        if bar.h >= s.sl:
            _close(tr, bar.t, s.sl * (1 + costs.stop_slip), "stop loss")
        elif bar.l <= s.tp:
            _close(tr, bar.t, s.tp * (1 + costs.tp_slip), "take profit")
        elif bar.t + 60000 >= tr.time_stop_ms:
            _close(tr, bar.t, bar.c * (1 + costs.exit_slip), "time stop")
    return "closed" if tr.state == "closed" else None


def _close(tr: SimTrade, t: int, price: float, reason: str) -> None:
    tr.state, tr.exit_ms, tr.exit_price, tr.exit_reason = "closed", t, price, reason


def summarize(trades: list[SimTrade], costs: SimCosts) -> dict:
    """Expectancy-first statistics in R (1R = the planned stop distance)."""
    closed = [t for t in trades if t.state == "closed"]
    rs = [t.r_net(costs) for t in closed]
    n = len(rs)
    if not n:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    streak = worst_streak = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if r <= 0 else 0
        worst_streak = max(worst_streak, streak)
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / (n - 1) if n > 1 else 0.0
    se = (var / n) ** 0.5
    return {
        "trades":        n,
        "win_rate":      len(wins) / n,
        "avg_win_r":     sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_r":    sum(losses) / len(losses) if losses else 0.0,
        "expectancy_r":  mean,
        "t_stat":        mean / se if se else 0.0,
        "total_r":       sum(rs),
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float("inf"),
        "max_dd_r":      dd,
        "worst_losing_streak": worst_streak,
        "exits":         {k: sum(1 for t in closed if t.exit_reason == k)
                          for k in ("take profit", "stop loss", "time stop")},
    }
