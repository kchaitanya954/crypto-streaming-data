"""
Tests for signals/adaptive_strategy.py — Kelly sizing, drawdown guards,
consecutive-loss filter, sell ratios, and state serialisation.
"""
import pytest
from signals.adaptive_strategy import AdaptiveState, BUY_BASE_PCT


def _make_trades(pnl_list):
    """Build minimal trade dicts from a list of P&L percentages."""
    return [{"pnl_pct": p} for p in pnl_list]


# ── buy_pct_for: base sizing ──────────────────────────────────────────────────

def test_buy_pct_high_confidence_default():
    s = AdaptiveState()
    pct = s.buy_pct_for("HIGH", adx_val=30.0)
    assert 10.0 <= pct <= 90.0


def test_buy_pct_low_confidence_lower_than_high():
    s = AdaptiveState()
    high = s.buy_pct_for("HIGH",   adx_val=30.0)
    low  = s.buy_pct_for("LOW",    adx_val=30.0)
    assert low < high


def test_buy_pct_minimum_floor():
    """Even with Kelly=0 and max drawdown, floor is 10%."""
    s = AdaptiveState()
    s.current_drawdown_pct = 30.0
    s.consecutive_losses   = 10
    s.perf_multiplier      = 0.25
    pct = s.buy_pct_for("LOW", adx_val=10.0)
    assert pct >= 10.0


def test_buy_pct_maximum_cap():
    """Even with great stats, cap is 90%."""
    s = AdaptiveState()
    s.win_rate     = 0.9
    s.avg_win_pct  = 10.0
    s.avg_loss_pct = 0.1
    s.perf_multiplier = 2.0
    pct = s.buy_pct_for("HIGH", adx_val=50.0)
    assert pct <= 90.0


def test_buy_pct_high_adx_boosts():
    s = AdaptiveState()
    normal = s.buy_pct_for("HIGH", adx_val=25.0)
    boosted = s.buy_pct_for("HIGH", adx_val=45.0)
    assert boosted >= normal


def test_buy_pct_drawdown_20_reduces():
    s = AdaptiveState()
    s.current_drawdown_pct = 25.0
    pct = s.buy_pct_for("HIGH", adx_val=30.0)
    s2 = AdaptiveState()
    pct_no_dd = s2.buy_pct_for("HIGH", adx_val=30.0)
    assert pct < pct_no_dd


def test_buy_pct_consecutive_losses_3_reduces():
    s = AdaptiveState()
    s.consecutive_losses = 3
    pct_with = s.buy_pct_for("MEDIUM", adx_val=30.0)
    s2 = AdaptiveState()
    pct_without = s2.buy_pct_for("MEDIUM", adx_val=30.0)
    assert pct_with <= pct_without


# ── sell_ratio_for ────────────────────────────────────────────────────────────

def test_sell_ratio_high_is_100():
    s = AdaptiveState()
    assert s.sell_ratio_for("HIGH") == pytest.approx(1.0)


def test_sell_ratio_medium_default():
    s = AdaptiveState()
    assert s.sell_ratio_for("MEDIUM") == pytest.approx(0.6)


def test_sell_ratio_drawdown_increases_sell():
    s = AdaptiveState()
    s.current_drawdown_pct = 15.0
    ratio = s.sell_ratio_for("MEDIUM")
    assert ratio > 0.6


def test_sell_ratio_never_exceeds_1():
    s = AdaptiveState()
    s.current_drawdown_pct = 50.0
    s.consecutive_losses   = 10
    assert s.sell_ratio_for("LOW") <= 1.0


# ── kelly_fraction ────────────────────────────────────────────────────────────

def test_kelly_zero_when_no_wins():
    s = AdaptiveState()
    s.trades_analyzed = 10   # past the _MIN_TRADES threshold
    s.win_rate     = 0.0
    s.avg_win_pct  = 0.0
    s.avg_loss_pct = 1.0
    # avg_win_pct == 0 → conservative fallback, not full zero
    assert s.kelly_fraction() < 0.05


def test_kelly_positive_when_winning():
    s = AdaptiveState()
    s.win_rate     = 0.6
    s.avg_win_pct  = 3.0
    s.avg_loss_pct = 1.0
    assert s.kelly_fraction() > 0


# ── update ────────────────────────────────────────────────────────────────────

def test_update_win_rate():
    s = AdaptiveState()
    trades = _make_trades([1.0, -1.0, 1.0, 1.0])  # 3 wins, 1 loss
    s.update(trades)
    assert s.win_rate == pytest.approx(0.75)


def test_update_consecutive_losses():
    s = AdaptiveState()
    trades = _make_trades([1.0, -1.0, -1.0, -1.0])
    s.update(trades)
    assert s.consecutive_losses == 3


def test_update_consecutive_losses_reset_on_win():
    s = AdaptiveState()
    trades = _make_trades([-1.0, -1.0, 1.0])
    s.update(trades)
    assert s.consecutive_losses == 0


def test_update_perf_multiplier_penalised_on_losses():
    s = AdaptiveState()
    trades = _make_trades([-2.0] * 10)
    s.update(trades)
    assert s.perf_multiplier < 1.0


def test_update_perf_multiplier_rewarded_on_wins():
    s = AdaptiveState()
    trades = _make_trades([3.0] * 10)
    s.update(trades)
    assert s.perf_multiplier > 1.0


# ── serialisation ─────────────────────────────────────────────────────────────

def test_to_dict_from_dict_roundtrip():
    s = AdaptiveState()
    s.win_rate            = 0.55
    s.consecutive_losses  = 2
    s.current_drawdown_pct = 8.5
    d = s.to_dict()

    s2 = AdaptiveState()
    s2.from_dict(d)
    assert s2.win_rate             == pytest.approx(0.55)
    assert s2.consecutive_losses   == 2
    assert s2.current_drawdown_pct == pytest.approx(8.5)


def test_circuit_breaker_triggers_at_3_losses():
    s = AdaptiveState()
    s.consecutive_losses = 3
    info = s.summary()
    assert info["circuit_breaker"] is True


def test_circuit_breaker_triggers_at_10_pct_dd():
    s = AdaptiveState()
    s.current_drawdown_pct = 10.0
    info = s.summary()
    assert info["circuit_breaker"] is True


def test_circuit_breaker_off_normally():
    s = AdaptiveState()
    info = s.summary()
    assert info["circuit_breaker"] is False
