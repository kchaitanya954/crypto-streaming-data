"""
Adaptive position-sizing engine.

Algorithm: Half-Kelly Criterion + Performance Feedback Loop

Buy sizing:
  1. Compute Kelly fraction from rolling 20-trade win/loss stats
  2. Apply Half-Kelly (f*/2) for safety
  3. Scale by: confidence weight × ADX strength × performance multiplier × drawdown guard

Sell sizing:
  - Confidence-weighted partial exits (HIGH=100%, MEDIUM=60%, LOW=30%)
  - Tightened during drawdown (exit more aggressively when portfolio down)

Performance feedback (runs every 5 min):
  - Rolling win rate, avg gain, avg loss → new Kelly base
  - Consecutive loss counter → circuit breaker (step down size)
  - Portfolio drawdown from peak → further size reduction
  - Recovery: size gradually restored as performance improves
"""

import time
from dataclasses import dataclass, field
from typing import Optional

_WINDOW      = 20    # rolling trade window
_MIN_TRADES  = 5     # min trades before Kelly kicks in

# BUY: percentage of *remaining* trigger budget to deploy per signal.
# These are base values by confidence; Kelly/drawdown/ADX scale them.
# Example: $10 trigger, MEDIUM signal → deploy 50% of remaining = $5 first trade,
# then 50% of $5 = $2.50 second trade, etc. (geometric scaling).
BUY_BASE_PCT = {"HIGH": 70.0, "MEDIUM": 50.0, "LOW": 30.0}
_MAX_BUY_PCT = 90.0   # never deploy more than 90% of remaining in one trade
_MIN_BUY_PCT = 10.0   # always deploy at least 10% of remaining

# SELL: fraction of *coins held by trigger* to sell per signal.
SELL_BASE = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}


@dataclass
class AdaptiveState:
    # Kelly inputs (rolling)
    win_rate:     float = 0.50   # fraction of winning trades
    avg_win_pct:  float = 2.0    # avg % gain on winning trades
    avg_loss_pct: float = 1.0    # avg % loss on losing trades (positive)

    # Performance feedback
    perf_multiplier: float = 1.0  # 0.25 – 2.0

    # Drawdown tracking
    peak_portfolio:       float = 0.0
    current_drawdown_pct: float = 0.0
    consecutive_losses:   int   = 0

    # Meta
    trades_analyzed: int   = 0
    last_updated:    float = 0.0

    # Internal: recent P&Ls (not persisted, rebuilt each cycle)
    recent_pnls: list = field(default_factory=list, repr=False)

    # ── Kelly ─────────────────────────────────────────────────────────────

    def kelly_fraction(self) -> float:
        """
        Half-Kelly position size as fraction of portfolio.
        Formula: f* = (W*b - L) / b,  b = avg_win / avg_loss
        Returns Half-Kelly capped at 15%.
        """
        if self.trades_analyzed < _MIN_TRADES:
            return 0.05  # conservative 5% default until enough data

        b = self.avg_win_pct / max(self.avg_loss_pct, 0.01)
        W = self.win_rate
        L = 1.0 - W
        kelly = (W * b - L) / b
        half_kelly = max(0.0, kelly * 0.5)
        return min(half_kelly, 0.15)  # cap at 15%

    # ── Position sizing ───────────────────────────────────────────────────

    def buy_pct_for(self, confidence: str, adx_val: Optional[float] = None) -> float:
        """
        Recommended % of *remaining trigger budget* to deploy for this signal.

        Base by confidence: HIGH=70%, MEDIUM=50%, LOW=30%
        Scaled by:
          - Kelly performance (0.5x–1.5x)
          - ADX strength: <20→0.8x, 20-30→1.0x, 30-40→1.1x, 40+→1.2x
          - Perf multiplier: 0.25–2.0
          - Drawdown guard: DD>=5%→0.8x, >=10%→0.6x, >=20%→0.3x
          - Consecutive losses: 3+→0.6x, 5+→0.3x
        Clamped to [10%, 90%] so at least 10% is always deployed and
        at most 90% of remaining — preserving budget for future signals.
        """
        base = BUY_BASE_PCT.get(confidence.upper(), 50.0)

        # Kelly scales the base: good track record → deploy more aggressively
        kelly = self.kelly_fraction()         # 0.0 – 0.15
        kelly_scale = 0.5 + kelly / 0.15 * 1.0  # maps 0→0.5, 0.075→1.0, 0.15→1.5
        kelly_scale = max(0.5, min(1.5, kelly_scale))

        adx_mult = 1.0
        if adx_val is not None:
            if adx_val >= 40:
                adx_mult = 1.20
            elif adx_val >= 30:
                adx_mult = 1.10
            elif adx_val < 20:
                adx_mult = 0.80

        dd_mult = 1.0
        if self.current_drawdown_pct >= 20:
            dd_mult = 0.30
        elif self.current_drawdown_pct >= 10:
            dd_mult = 0.60
        elif self.current_drawdown_pct >= 5:
            dd_mult = 0.80

        if self.consecutive_losses >= 5:
            dd_mult = min(dd_mult, 0.30)
        elif self.consecutive_losses >= 3:
            dd_mult = min(dd_mult, 0.60)

        pct = base * kelly_scale * self.perf_multiplier * adx_mult * dd_mult
        return round(max(_MIN_BUY_PCT, min(pct, _MAX_BUY_PCT)), 2)

    def sell_ratio_for(self, confidence: str) -> float:
        """Fraction of position to sell (0–1). More aggressive during drawdown."""
        base = SELL_BASE.get(confidence.upper(), 0.6)
        if self.current_drawdown_pct >= 10:
            base = min(1.0, base + 0.2)
        elif self.consecutive_losses >= 3:
            base = min(1.0, base + 0.1)
        return round(base, 2)

    # ── Update logic ──────────────────────────────────────────────────────

    def update(self, completed_trades: list) -> None:
        """
        Re-compute state from completed {pnl_pct, portfolio_val} trades.
        Called by the adaptive monitor loop every cycle.
        """
        if not completed_trades:
            return

        self.trades_analyzed = len(completed_trades)
        window = completed_trades[-_WINDOW:]

        pnls   = [t["pnl_pct"] for t in window]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        self.recent_pnls  = pnls
        self.win_rate     = len(wins) / len(pnls) if pnls else 0.5
        self.avg_win_pct  = (sum(wins) / len(wins)) if wins else 0.0
        self.avg_loss_pct = abs(sum(losses) / len(losses)) if losses else 0.5

        # Drawdown tracking from portfolio value series
        if completed_trades and "portfolio_val" in completed_trades[0]:
            vals = [t["portfolio_val"] for t in completed_trades if "portfolio_val" in t]
            if vals:
                self.peak_portfolio = max(self.peak_portfolio, max(vals))
                last_val = vals[-1]
                if self.peak_portfolio > 0:
                    self.current_drawdown_pct = max(
                        0.0,
                        (self.peak_portfolio - last_val) / self.peak_portfolio * 100
                    )

        # Consecutive loss streak (most recent first)
        self.consecutive_losses = 0
        for p in reversed(pnls):
            if p < 0:
                self.consecutive_losses += 1
            else:
                break

        # Performance multiplier: reward/penalise based on rolling avg P&L
        recent_avg = sum(pnls) / len(pnls) if pnls else 0.0
        if recent_avg >= 2.0:
            self.perf_multiplier = min(2.0, 1.0 + recent_avg / 10)
        elif recent_avg <= -1.0:
            self.perf_multiplier = max(0.25, 1.0 + recent_avg / 5)
        else:
            # Smooth recovery toward 1.0
            target = 1.0 + recent_avg / 20
            self.perf_multiplier = round(0.8 * self.perf_multiplier + 0.2 * target, 4)

        self.last_updated = time.time()

    def to_dict(self) -> dict:
        """Serialize for DB persistence (excludes recent_pnls)."""
        return {
            "win_rate":             self.win_rate,
            "avg_win_pct":          self.avg_win_pct,
            "avg_loss_pct":         self.avg_loss_pct,
            "perf_multiplier":      self.perf_multiplier,
            "peak_portfolio":       self.peak_portfolio,
            "current_drawdown_pct": self.current_drawdown_pct,
            "consecutive_losses":   self.consecutive_losses,
            "trades_analyzed":      self.trades_analyzed,
            "last_updated":         self.last_updated,
        }

    def from_dict(self, d: dict) -> None:
        """Restore from DB-persisted dict."""
        for k, v in d.items():
            if hasattr(self, k):
                setattr(self, k, v)

    def summary(self) -> dict:
        """Return clean dict for /api/adaptive and UI display."""
        kelly = self.kelly_fraction()
        return {
            "trades_analyzed":    self.trades_analyzed,
            "win_rate_pct":       round(self.win_rate * 100, 1),
            "avg_win_pct":        round(self.avg_win_pct, 3),
            "avg_loss_pct":       round(self.avg_loss_pct, 3),
            "kelly_fraction_pct": round(kelly * 100, 2),
            "perf_multiplier":    round(self.perf_multiplier, 3),
            "drawdown_pct":       round(self.current_drawdown_pct, 2),
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker":    self.consecutive_losses >= 3 or self.current_drawdown_pct >= 10,
            "rec_buy": {
                "HIGH":   self.buy_pct_for("HIGH",   40.0),
                "MEDIUM": self.buy_pct_for("MEDIUM", 30.0),
                "LOW":    self.buy_pct_for("LOW",    20.0),
            },
            "rec_sell": {
                "HIGH":   self.sell_ratio_for("HIGH"),
                "MEDIUM": self.sell_ratio_for("MEDIUM"),
                "LOW":    self.sell_ratio_for("LOW"),
            },
            "last_updated": self.last_updated,
        }


# Module-level singleton — shared across all server handlers
engine = AdaptiveState()
