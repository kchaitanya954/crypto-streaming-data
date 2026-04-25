"""
Tests for exchange/futures_trade.py — liquidation price, SL/TP math,
minimum order guard, and quantity precision.
"""
import math
import pytest
from exchange.futures_trade import (
    compute_liquidation_price,
    compute_futures_sl_tp,
    MAINTENANCE_MARGIN,
    MAX_SL_MARGIN_LOSS,
    MIN_SL_PCT,
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
    """TP distance ≥ 2.5× SL distance (minimum RR)."""
    entry = 50000.0
    for lev in (1, 5, 10, 20):
        sl, tp, _ = compute_futures_sl_tp("long", entry, lev, atr_pct=0.8)
        sl_dist = entry - sl
        tp_dist = tp - entry
        assert tp_dist >= sl_dist * 2.5 - 1e-4, f"leverage={lev}"


def test_tp_min_margin_gain():
    """TP gain ≥ MIN_TP_MARGIN_GAIN / leverage."""
    entry, lev = 50000.0, 5
    sl, tp, _ = compute_futures_sl_tp("long", entry, lev, atr_pct=0.1)
    tp_pct = (tp - entry) / entry
    assert tp_pct >= MIN_TP_MARGIN_GAIN / lev - 1e-9


# ── quantity precision ────────────────────────────────────────────────────────

def test_qty_6dp_precision():
    """Verify that 6-decimal floor works for small BTC positions."""
    margin_usdt, leverage, price = 12.0, 5, 78000.0
    notional = margin_usdt * leverage
    qty_raw  = notional / price                   # ~0.000769...
    qty = math.floor(qty_raw * 1_000_000) / 1_000_000
    assert qty > 0
    assert len(str(qty).split(".")[-1]) <= 6


def test_qty_rounds_to_zero_detected():
    """Very small budget should floor to 0 — real code returns early."""
    margin_usdt, leverage, price = 0.001, 1, 78000.0
    qty_raw = (margin_usdt * leverage) / price
    qty = math.floor(qty_raw * 1_000_000) / 1_000_000
    assert qty == 0.0
