"""
Tests for indicators/indicators.py — SMA, EMA, MACD, RSI, Stochastic, OBV, ATR.
"""
import pytest
from indicators.indicators import sma, ema, atr


# ── SMA ───────────────────────────────────────────────────────────────────────

def test_sma_basic():
    result = sma([1, 2, 3, 4, 5], period=3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)
    assert result[3] == pytest.approx(3.0)
    assert result[4] == pytest.approx(4.0)


def test_sma_period_1():
    prices = [10.0, 20.0, 30.0]
    result = sma(prices, period=1)
    assert result == pytest.approx([10.0, 20.0, 30.0])


def test_sma_period_equals_length():
    prices = [1.0, 2.0, 3.0]
    result = sma(prices, period=3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)


def test_sma_shorter_than_period():
    result = sma([1.0, 2.0], period=5)
    assert all(v is None for v in result)


def test_sma_invalid_period():
    with pytest.raises(ValueError):
        sma([1.0, 2.0], period=0)


# ── EMA ───────────────────────────────────────────────────────────────────────

def test_ema_basic():
    prices = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ema(prices, period=3)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(2.0)          # seed = SMA(1,2,3) = 2.0
    mult = 2 / (3 + 1)
    expected = (4.0 - 2.0) * mult + 2.0             # prices[3]=4.0 → 3.0
    assert result[3] == pytest.approx(expected)


def test_ema_period_1():
    prices = [5.0, 10.0, 15.0]
    result = ema(prices, period=1)
    assert result == pytest.approx([5.0, 10.0, 15.0])


def test_ema_shorter_than_period():
    result = ema([1.0, 2.0], period=5)
    assert all(v is None for v in result)


def test_ema_invalid_period():
    with pytest.raises(ValueError):
        ema([1.0], period=0)


# ── ATR ───────────────────────────────────────────────────────────────────────

def test_atr_basic():
    highs  = [11.0, 12.0, 13.0, 14.0, 15.0,
              16.0, 17.0, 18.0, 19.0, 20.0,
              21.0, 22.0, 23.0, 24.0, 25.0]
    lows   = [9.0,  10.0, 11.0, 12.0, 13.0,
              14.0, 15.0, 16.0, 17.0, 18.0,
              19.0, 20.0, 21.0, 22.0, 23.0]
    closes = [10.0, 11.0, 12.0, 13.0, 14.0,
              15.0, 16.0, 17.0, 18.0, 19.0,
              20.0, 21.0, 22.0, 23.0, 24.0]
    result = atr(highs, lows, closes, period=14)
    assert result[13] is None                        # indices 0-13 are None
    assert result[14] is not None                    # first value at index=period
    assert result[14] > 0


def test_atr_constant_price():
    """Zero range candles → ATR should be 0."""
    n = 20
    highs  = [100.0] * n
    lows   = [100.0] * n
    closes = [100.0] * n
    result = atr(highs, lows, closes, period=14)
    valid  = [v for v in result if v is not None]
    assert all(v == pytest.approx(0.0) for v in valid)


def test_atr_insufficient_data():
    result = atr([10.0, 11.0], [9.0, 10.0], [10.0, 10.5], period=14)
    assert all(v is None for v in result)
