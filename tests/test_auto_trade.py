"""
Tests for exchange/auto_trade.py — market constraints, min-order guard,
quantity step-flooring, and error-message parsing.
"""
import pytest
from exchange.auto_trade import (
    _apply_constraints,
    _get_market_constraints,
    _learn_min_qty_from_error,
    _market_cache,
    MIN_ORDER_USDT,
)


# ── _apply_constraints ────────────────────────────────────────────────────────

def test_apply_constraints_floors_to_step():
    c = {"step": 0.01, "min_qty": 0.01}
    assert _apply_constraints(1.239, c) == pytest.approx(1.23)


def test_apply_constraints_exact_multiple():
    c = {"step": 0.001, "min_qty": 0.001}
    assert _apply_constraints(0.005, c) == pytest.approx(0.005)


def test_apply_constraints_btc_step():
    c = _get_market_constraints("BTC")     # step = 0.00001
    raw = 0.000769
    floored = _apply_constraints(raw, c)
    assert floored == pytest.approx(0.00076, rel=1e-4)


def test_apply_constraints_eth_step():
    c = _get_market_constraints("ETH")     # step = 0.0001
    assert _apply_constraints(0.00459, c) == pytest.approx(0.0045)


def test_apply_constraints_dust_floors_to_zero():
    c = {"step": 0.01, "min_qty": 0.01}
    assert _apply_constraints(0.001, c) == pytest.approx(0.0)


# ── _get_market_constraints ───────────────────────────────────────────────────

def test_known_base_btc():
    c = _get_market_constraints("BTC")
    assert c["min_qty"] == pytest.approx(0.00001)
    assert c["step"]    == pytest.approx(0.00001)


def test_known_base_eth():
    c = _get_market_constraints("ETH")
    assert c["min_qty"] == pytest.approx(0.0001)


def test_case_insensitive():
    assert _get_market_constraints("btc") == _get_market_constraints("BTC")


def test_unknown_base_returns_default():
    c = _get_market_constraints("UNKNOWN_COIN")
    assert c["min_qty"] > 0
    assert c["step"]    > 0


# ── _learn_min_qty_from_error ─────────────────────────────────────────────────

def test_learn_min_qty_parses_message():
    _learn_min_qty_from_error("XYZ", "Quantity should be greater than 0.00123")
    assert _market_cache["XYZ"]["min_qty"] == pytest.approx(0.00123)
    assert _market_cache["XYZ"]["step"]    == pytest.approx(0.00123)


def test_learn_min_qty_ignores_bad_message():
    before = _market_cache.get("ABC")
    _learn_min_qty_from_error("ABC", "Some unrelated error message")
    after = _market_cache.get("ABC")
    assert before == after   # unchanged


def test_learn_min_qty_case_insensitive_key():
    _learn_min_qty_from_error("aaa", "Quantity should be greater than 0.5")
    assert "AAA" in _market_cache
    assert _market_cache["AAA"]["min_qty"] == pytest.approx(0.5)


# ── MIN_ORDER_USDT constant ───────────────────────────────────────────────────

def test_min_order_usdt_value():
    """Minimum order size must be at least $10 (CoinDCX requirement)."""
    assert MIN_ORDER_USDT >= 10.0


def test_budget_50_at_10pct_below_minimum():
    """$50 budget × 10% adaptive = $5 < MIN_ORDER_USDT → should be rejected."""
    budget  = 50.0
    pct     = 10.0
    order   = budget * pct / 100.0
    assert order < MIN_ORDER_USDT


def test_budget_120_at_10pct_above_minimum():
    """$120 budget × 10% adaptive = $12 ≥ MIN_ORDER_USDT → should proceed."""
    budget  = 120.0
    pct     = 10.0
    order   = budget * pct / 100.0
    assert order >= MIN_ORDER_USDT
