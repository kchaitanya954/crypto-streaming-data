"""
Technical indicators: moving averages (SMA, EMA), exponential MACD, and RSI.

All functions take a sequence of closing prices and return lists aligned by index.
Leading values where the indicator is not yet defined are None.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Union


def sma(prices: Sequence[float], period: int) -> List[Optional[float]]:
    """
    Simple Moving Average.

    Args:
        prices: Closing prices (oldest first).
        period: Number of periods.

    Returns:
        List of same length as prices; first (period - 1) values are None.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(prices)
    out: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        out[i] = sum(prices[i - period + 1 : i + 1]) / period
    return out


def ema(prices: Sequence[float], period: int) -> List[Optional[float]]:
    """
    Exponential Moving Average.

    Uses multiplier 2 / (period + 1). First value is SMA of first `period` prices,
    then EMA_t = (close_t - EMA_{t-1}) * multiplier + EMA_{t-1}.

    Args:
        prices: Closing prices (oldest first).
        period: Number of periods.

    Returns:
        List of same length as prices; first (period - 1) values are None.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(prices)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    mult = 2.0 / (period + 1)
    # First EMA value = SMA of first `period` prices
    out[period - 1] = sum(prices[:period]) / period
    for i in range(period, n):
        out[i] = (prices[i] - out[i - 1]) * mult + out[i - 1]  # type: ignore
    return out


@dataclass
class MACDResult:
    """MACD line, signal line, and histogram."""

    macd_line: List[Optional[float]]
    signal_line: List[Optional[float]]
    histogram: List[Optional[float]]


def macd(
    prices: Sequence[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """
    Exponential MACD: MACD line = EMA(fast) - EMA(slow), Signal = EMA(MACD), Histogram = MACD - Signal.

    Args:
        prices: Closing prices (oldest first).
        fast_period: Fast EMA period (default 12).
        slow_period: Slow EMA period (default 26).
        signal_period: Signal line EMA period (default 9).

    Returns:
        MACDResult with macd_line, signal_line, histogram (all same length as prices).
    """
    if slow_period <= fast_period:
        raise ValueError("slow_period must be > fast_period")
    fast_ema = ema(prices, fast_period)
    slow_ema = ema(prices, slow_period)
    n = len(prices)
    macd_line: List[Optional[float]] = [None] * n
    for i in range(slow_period - 1, n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]
    # Signal = EMA of MACD line (only over defined MACD values)
    macd_values = [macd_line[i] for i in range(n) if macd_line[i] is not None]
    signal_ema = ema(macd_values, signal_period)
    signal_line: List[Optional[float]] = [None] * n
    j = 0
    for i in range(slow_period - 1, n):
        if macd_line[i] is not None:
            signal_line[i] = signal_ema[j]
            j += 1
    histogram = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]
    return MACDResult(macd_line=macd_line, signal_line=signal_line, histogram=histogram)


def rsi(prices: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """
    Relative Strength Index using Wilder's smoothing (smoothed averages of gains and losses).

    RS = avg_gain / avg_loss, RSI = 100 - 100 / (1 + RS).
    First avg_gain/avg_loss are SMA of first `period` gains/losses; then
    avg_gain = (prev_avg_gain * (period - 1) + gain) / period (same for loss).

    Args:
        prices: Closing prices (oldest first).
        period: RSI period (default 14).

    Returns:
        List of same length as prices; first `period` values are None.
    """
    if period < 2:
        raise ValueError("period must be >= 2")
    n = len(prices)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, n):
        ch = prices[i] - prices[i - 1]
        gains.append(ch if ch > 0 else 0.0)
        losses.append(-ch if ch < 0 else 0.0)
    # First RSI at index `period`: use SMA of first `period` gains and losses
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    # Wilder's smoothing for the rest
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out
