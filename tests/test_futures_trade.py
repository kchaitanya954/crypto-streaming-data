"""
Tests for exchange/futures_trade.py — liquidation price, SL/TP math,
minimum order guard, and quantity precision.
"""
import math
import pytest
from exchange.futures_trade import (
    compute_liquidation_price,
    compute_futures_sl_tp,
    _order_id_from_result,
    _FUTURES_STEP_QTY,
    _learn_futures_min_qty,
    MAINTENANCE_MARGIN,
    MAX_SL_MARGIN_LOSS,
    MIN_SL_PCT,
    MIN_RR_RATIO,
    LIQ_BUFFER_RATIO,
    MIN_TP_MARGIN_GAIN,
)


# ── compute_liquidation_price ─────────────────────────────────────────────────

def test_liq_long_below_entry():
    liq = compute_liquidation_price("long", entry_price=50000.0, leverage=10)
    assert liq < 50000.0


def test_liq_short_above_entry():
    liq = compute_liquidation_price("short", entry_price=50000.0, leverage=10)
    assert liq > 50000.0


def test_liq_long_formula():
    entry, lev = 50000.0, 10
    expected = entry * (1.0 - (1.0 - MAINTENANCE_MARGIN) / lev)
    assert compute_liquidation_price("long", entry, lev) == pytest.approx(expected, rel=1e-6)


def test_liq_short_formula():
    entry, lev = 50000.0, 10
    expected = entry * (1.0 + (1.0 - MAINTENANCE_MARGIN) / lev)
    assert compute_liquidation_price("short", entry, lev) == pytest.approx(expected, rel=1e-6)


def test_liq_leverage_1_long():
    liq = compute_liquidation_price("long", entry_price=1000.0, leverage=1)
    assert liq < 1000.0
    # At leverage=1, loss ≈ 99.5% of entry before liquidation
    assert liq == pytest.approx(1000.0 * MAINTENANCE_MARGIN, rel=1e-4)


# ── compute_futures_sl_tp ─────────────────────────────────────────────────────

@pytest.mark.parametrize("side", ["long", "short"])
def test_sl_tp_returns_three_values(side):
    sl, tp, liq = compute_futures_sl_tp(side, 50000.0, leverage=5, atr_pct=0.8)
    assert all(v > 0 for v in (sl, tp, liq))


def test_long_sl_below_entry():
    sl, tp, liq = compute_futures_sl_tp("long", 50000.0, leverage=5, atr_pct=0.8)
    assert sl < 50000.0


def test_long_tp_above_entry():
    sl, tp, liq = compute_futures_sl_tp("long", 50000.0, leverage=5, atr_pct=0.8)
    assert tp > 50000.0


def test_short_sl_above_entry():
    sl, tp, liq = compute_futures_sl_tp("short", 50000.0, leverage=5, atr_pct=0.8)
    assert sl > 50000.0


def test_short_tp_below_entry():
    sl, tp, liq = compute_futures_sl_tp("short", 50000.0, leverage=5, atr_pct=0.8)
    assert tp < 50000.0


def test_sl_never_tighter_than_min():
    """SL distance ≥ MIN_SL_PCT regardless of ATR."""
    sl, tp, liq = compute_futures_sl_tp("long", 10000.0, leverage=10, atr_pct=0.0)
    sl_pct = (10000.0 - sl) / 10000.0
    assert sl_pct >= MIN_SL_PCT - 1e-9


def test_sl_within_liquidation_buffer():
    """SL must stay within LIQ_BUFFER_RATIO of liquidation distance from entry."""
    entry, lev = 50000.0, 10
    sl, tp, liq = compute_futures_sl_tp("long", entry, lev, atr_pct=0.5)
    liq_dist_pct = (1.0 - MAINTENANCE_MARGIN) / lev
    max_sl_pct   = liq_dist_pct * LIQ_BUFFER_RATIO
    sl_pct_actual = (entry - sl) / entry
    assert sl_pct_actual <= max_sl_pct + 1e-9


def test_sl_margin_loss_cap():
    """SL loss ≤ MAX_SL_MARGIN_LOSS × entry."""
    entry, lev = 50000.0, 5
    sl, _, _ = compute_futures_sl_tp("long", entry, lev, atr_pct=5.0)
    sl_pct = (entry - sl) / entry
    assert sl_pct <= MAX_SL_MARGIN_LOSS / lev + 1e-9


def test_tp_min_risk_reward():
    """TP distance ≥ MIN_RR_RATIO × SL distance."""
    entry = 50000.0
    for lev in (1, 5, 10, 20):
        sl, tp, _ = compute_futures_sl_tp("long", entry, lev, atr_pct=0.8)
        sl_dist = entry - sl
        tp_dist = tp - entry
        assert tp_dist >= sl_dist * MIN_RR_RATIO - 1e-4, f"leverage={lev}"


def test_tp_min_margin_gain():
    """TP gain ≥ MIN_TP_MARGIN_GAIN / leverage."""
    entry, lev = 50000.0, 5
    sl, tp, _ = compute_futures_sl_tp("long", entry, lev, atr_pct=0.1)
    tp_pct = (tp - entry) / entry
    assert tp_pct >= MIN_TP_MARGIN_GAIN / lev - 1e-9


# ── quantity step flooring ────────────────────────────────────────────────────

def test_qty_floored_to_btc_step():
    """BTC step=0.001: 0.001029 should floor to 0.001, not 0.001029."""
    step = _FUTURES_STEP_QTY["BTC"]
    qty_raw = 0.001029
    qty = math.floor(qty_raw / step) * step
    assert abs(qty - 0.001) < 1e-9


def test_qty_floored_below_step_is_zero():
    """0.000859 with step=0.001 floors to 0."""
    step = _FUTURES_STEP_QTY["BTC"]
    qty_raw = 0.000859
    qty = math.floor(qty_raw / step) * step
    assert qty == 0.0


def test_qty_exact_multiple_unchanged():
    """0.003 with step=0.001 stays 0.003."""
    step = _FUTURES_STEP_QTY["BTC"]
    qty_raw = 0.003
    qty = math.floor(qty_raw / step) * step
    assert abs(qty - 0.003) < 1e-9


# ── _order_id_from_result ─────────────────────────────────────────────────────

def test_order_id_from_dict():
    assert _order_id_from_result({"id": "abc123"}) == "abc123"


def test_order_id_from_list():
    """Futures API returns a list — must unwrap first element."""
    assert _order_id_from_result([{"id": "xyz789"}]) == "xyz789"


def test_order_id_from_empty_list():
    assert _order_id_from_result([]) == ""


def test_order_id_prefers_id_over_order_id():
    assert _order_id_from_result([{"id": "a", "order_id": "b"}]) == "a"


# ── _learn_futures_min_qty ────────────────────────────────────────────────────

def test_learn_from_divisible_by_error():
    _learn_futures_min_qty("SOL", "Quantity should be divisible by 0.1")
    assert _FUTURES_STEP_QTY["SOL"] == pytest.approx(0.1)


def test_learn_from_greater_than_error():
    _learn_futures_min_qty("DOGE", "Quantity should be greater than 10")
    assert _FUTURES_STEP_QTY["DOGE"] == pytest.approx(10.0)
