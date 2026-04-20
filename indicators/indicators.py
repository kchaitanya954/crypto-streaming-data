"""
Technical indicators: moving averages (SMA, EMA), MACD, RSI, Stochastic, and OBV.

All functions take a sequence of prices and return lists aligned by index.
Leading values where the indicator is not yet defined are None.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence


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


@dataclass
class StochasticResult:
    """Stochastic %K and %D lines."""

    k: List[Optional[float]]
    d: List[Optional[float]]


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    k_period: int = 14,
    d_period: int = 3,
) -> StochasticResult:
    """
    Stochastic Oscillator.

    %K = (close - lowest_low) / (highest_high - lowest_low) * 100  over k_period bars.
    %D = SMA(%K, d_period).

    Buy zone: %K crosses above %D when both are below 20 (oversold).
    Sell zone: %K crosses below %D when both are above 80 (overbought).

    Args:
        highs: High prices (oldest first).
        lows: Low prices (oldest first).
        closes: Closing prices (oldest first).
        k_period: Lookback window for %K (default 14).
        d_period: Smoothing period for %D (default 3).

    Returns:
        StochasticResult with k and d lists (same length as closes).
    """
    n = len(closes)
    k_line: List[Optional[float]] = [None] * n
    for i in range(k_period - 1, n):
        window_lows = lows[i - k_period + 1 : i + 1]
        window_highs = highs[i - k_period + 1 : i + 1]
        lowest = min(window_lows)
        highest = max(window_highs)
        denom = highest - lowest
        k_line[i] = ((closes[i] - lowest) / denom * 100.0) if denom != 0 else 50.0

    # %D = SMA of defined %K values, mapped back to full-length output
    k_values = [k_line[i] for i in range(n) if k_line[i] is not None]
    d_sma = sma(k_values, d_period)
    d_line: List[Optional[float]] = [None] * n
    j = 0
    for i in range(k_period - 1, n):
        if k_line[i] is not None:
            d_line[i] = d_sma[j]
            j += 1

    return StochasticResult(k=k_line, d=d_line)


def obv(closes: Sequence[float], volumes: Sequence[float]) -> List[float]:
    """
    On-Balance Volume.

    OBV rises when close > previous close (buying pressure) and falls otherwise.
    A rising OBV confirms an uptrend; falling OBV confirms a downtrend.

    Args:
        closes: Closing prices (oldest first).
        volumes: Volume per bar, same length as closes.

    Returns:
        List of OBV values (same length as closes). First value is 0.
    """
    n = len(closes)
    out: List[float] = [0.0] * n
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


@dataclass
class ADXResult:
    """ADX, +DI, and -DI lines."""

    adx:      List[Optional[float]]
    plus_di:  List[Optional[float]]
    minus_di: List[Optional[float]]


@dataclass
class BollingerResult:
    """Bollinger Bands: upper, middle (SMA), and lower bands."""

    upper:  List[Optional[float]]
    middle: List[Optional[float]]
    lower:  List[Optional[float]]


def bollinger_bands(
    prices: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> BollingerResult:
    """
    Bollinger Bands.

    middle = SMA(period), upper = middle + num_std * stddev,
    lower  = middle - num_std * stddev.

    Uses population standard deviation (divisor = period).

    Args:
        prices:  Closing prices (oldest first).
        period:  SMA period (default 20).
        num_std: Number of standard deviations for the bands (default 2.0).

    Returns:
        BollingerResult with upper, middle, lower (all same length as prices).
        First (period - 1) values are None.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    n = len(prices)
    upper:  List[Optional[float]] = [None] * n
    middle: List[Optional[float]] = [None] * n
    lower:  List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = prices[i - period + 1 : i + 1]
        mean   = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std    = variance ** 0.5
        middle[i] = mean
        upper[i]  = mean + num_std * std
        lower[i]  = mean - num_std * std
    return BollingerResult(upper=upper, middle=middle, lower=lower)


def adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> ADXResult:
    """
    Average Directional Index (ADX) with +DI and -DI.

    Measures trend strength (not direction). ADX > 25 = strong trend worth trading;
    ADX < 20 = choppy/sideways market where signals are unreliable.

    Uses Wilder's smoothing throughout (same as RSI).
    First ADX value appears at index 2 * period - 1.

    Args:
        highs:  High prices (oldest first).
        lows:   Low prices (oldest first).
        closes: Closing prices (oldest first).
        period: Smoothing period (default 14).

    Returns:
        ADXResult with adx, plus_di, minus_di (all same length as closes).
    """
    n = len(closes)
    adx_out:      List[Optional[float]] = [None] * n
    plus_di_out:  List[Optional[float]] = [None] * n
    minus_di_out: List[Optional[float]] = [None] * n

    if n < 2 * period:
        return ADXResult(adx=adx_out, plus_di=plus_di_out, minus_di=minus_di_out)

    # Compute True Range, +DM, -DM for each bar starting at index 1
    tr_vals:        List[float] = []
    plus_dm_vals:   List[float] = []
    minus_dm_vals:  List[float] = []

    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        tr_vals.append(tr)
        plus_dm_vals.append(up   if up > down and up > 0   else 0.0)
        minus_dm_vals.append(down if down > up and down > 0 else 0.0)

    # Wilder's initial sum (SMA seed) over first `period` values
    smooth_tr       = sum(tr_vals[:period])
    smooth_plus_dm  = sum(plus_dm_vals[:period])
    smooth_minus_dm = sum(minus_dm_vals[:period])

    def _di(dm: float, tr: float) -> float:
        return 100.0 * dm / tr if tr != 0 else 0.0

    dx_vals: List[float] = []

    # First DI/DX at original index `period`
    pdi = _di(smooth_plus_dm, smooth_tr)
    mdi = _di(smooth_minus_dm, smooth_tr)
    plus_di_out[period]  = pdi
    minus_di_out[period] = mdi
    denom = pdi + mdi
    dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom != 0 else 0.0)

    # Wilder's rolling smoothing for remaining bars
    for i in range(period, len(tr_vals)):
        smooth_tr       = smooth_tr       - smooth_tr / period       + tr_vals[i]
        smooth_plus_dm  = smooth_plus_dm  - smooth_plus_dm / period  + plus_dm_vals[i]
        smooth_minus_dm = smooth_minus_dm - smooth_minus_dm / period + minus_dm_vals[i]

        pdi = _di(smooth_plus_dm, smooth_tr)
        mdi = _di(smooth_minus_dm, smooth_tr)
        plus_di_out[i + 1]  = pdi
        minus_di_out[i + 1] = mdi
        denom = pdi + mdi
        dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom != 0 else 0.0)

    # ADX = Wilder's smoothing of DX; first value at original index 2*period-1
    adx_val = sum(dx_vals[:period]) / period
    adx_out[2 * period - 1] = adx_val
    for i in range(period, len(dx_vals)):
        adx_val = (adx_val * (period - 1) + dx_vals[i]) / period
        adx_out[i + period] = adx_val

    return ADXResult(adx=adx_out, plus_di=plus_di_out, minus_di=minus_di_out)


def atr(
    highs:  Sequence[float],
    lows:   Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> List[Optional[float]]:
    """
    Average True Range (Wilder smoothing).

    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = Wilder EMA(14) of TR values.

    Returns list aligned with input; first `period` values are None.
    """
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    tr_vals: List[float] = []
    for i in range(1, n):
        tr = max(
            highs[i]  - lows[i],
            abs(highs[i]  - closes[i - 1]),
            abs(lows[i]   - closes[i - 1]),
        )
        tr_vals.append(tr)

    # Seed with SMA of first `period` TR values
    atr_val = sum(tr_vals[:period]) / period
    out[period] = atr_val
    for i in range(period, len(tr_vals)):
        atr_val = (atr_val * (period - 1) + tr_vals[i]) / period
        out[i + 1] = atr_val

    return out
