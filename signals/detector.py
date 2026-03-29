"""
Signal detector: combines trend gates and entry indicators to produce buy/sell signals.

Signal flow:
  1. EMA(200) hard gate      — blocks signals against the long-term trend
  2. ADX gate                — blocks signals when market is choppy (ADX < threshold)
  3. MACD crossover          — trigger, must exceed min_histogram gap to avoid micro-crosses
  4. Bollinger Band gate     — BUY only below middle band (room to rise), SELL only above it
  5. Cooldown gate           — blocks signals within cooldown_bars of the previous signal
  6. RSI                     — confirmation (+1 if in healthy zone: 30–65 for BUY, 35–70 for SELL)
  7. Stochastic              — confirmation (+1 if %K/%D cross AND coming from right zone)
  8. OBV                     — confirmation (+1 if volume backs the move)
  9. min_confirmations gate — signal suppressed if too few confirmations pass
"""

from dataclasses import dataclass, field
from typing import Optional

from indicators import macd, rsi, stochastic, obv, ema, adx, bollinger_bands
from streaming.stream import Kline


# ── Indicator snapshot (one per closed candle, used by UI) ────────────────────

@dataclass
class IndicatorSnapshot:
    """All indicator values for a single closed candle — used by the UI server."""
    time:        int             # Unix seconds (open_time // 1000)
    open:        float
    high:        float
    low:         float
    close:       float
    volume:      float
    ema50:       Optional[float]
    ema200:      Optional[float]
    macd_line:   Optional[float]
    macd_signal: Optional[float]
    macd_hist:   Optional[float]
    rsi_val:     Optional[float]
    adx_val:     Optional[float]
    bb_upper:    Optional[float] = None
    bb_middle:   Optional[float] = None
    bb_lower:    Optional[float] = None


# ── Default params ────────────────────────────────────────────────────────────

MACD_FAST         = 12
MACD_SLOW         = 26
MACD_SIGNAL       = 9
EMA_FAST          = 50
EMA_SLOW          = 200
RSI_PERIOD        = 14
STOCH_K           = 14
STOCH_D           = 3
ADX_PERIOD        = 14
ADX_THRESHOLD     = 20.0   # below this = choppy market, skip signal
MIN_HISTOGRAM     = 0.1    # min |MACD − signal| to reject micro-crossovers (tune per asset/TF)
MIN_CONFIRMATIONS = 1      # allow LOW signals through (1 confirm)
BB_PERIOD         = 20     # Bollinger Band lookback
BB_STD            = 2.0    # Bollinger Band standard deviation multiplier
COOLDOWN_BARS     = 5      # bars to wait after a signal before allowing the next one


# ── Output dataclass ──────────────────────────────────────────────────────────

@dataclass
class Signal:
    """A buy or sell signal produced by the detector."""

    direction:  str            # "BUY" or "SELL"
    confidence: str            # "HIGH" | "MEDIUM" | "LOW"
    entry_price: float
    open_time:  int
    macd_val:   float
    signal_val: float
    histogram:  float
    adx_val:    float
    trend_note: str
    reasons:    list[str] = field(default_factory=list)


# ── Detector ──────────────────────────────────────────────────────────────────

class SignalDetector:
    """
    Stateful detector that ingests closed candles one at a time and returns a
    Signal whenever a valid buy/sell setup is detected, or None otherwise.

    Key quality filters:
      - adx_threshold (default 30): skip signals in ranging/choppy markets
      - min_histogram (default 0.5): skip hairline MACD crossovers
      - min_confirmations (default 2): require at least MEDIUM confidence

    Usage:
        detector = SignalDetector()
        detector.seed(historical_klines)
        signal = detector.update(new_kline)   # call on each closed candle
    """

    def __init__(
        self,
        macd_fast:         int   = MACD_FAST,
        macd_slow:         int   = MACD_SLOW,
        macd_signal:       int   = MACD_SIGNAL,
        ema_fast:          int   = EMA_FAST,
        ema_slow:          int   = EMA_SLOW,
        rsi_period:        int   = RSI_PERIOD,
        stoch_k:           int   = STOCH_K,
        stoch_d:           int   = STOCH_D,
        adx_period:        int   = ADX_PERIOD,
        adx_threshold:     float = ADX_THRESHOLD,
        min_histogram:     float = MIN_HISTOGRAM,
        min_confirmations: int   = MIN_CONFIRMATIONS,
        bb_period:         int   = BB_PERIOD,
        bb_std:            float = BB_STD,
        cooldown_bars:     int   = COOLDOWN_BARS,
    ) -> None:
        self.macd_fast         = macd_fast
        self.macd_slow         = macd_slow
        self.macd_signal       = macd_signal
        self.ema_fast          = ema_fast
        self.ema_slow          = ema_slow
        self.rsi_period        = rsi_period
        self.stoch_k           = stoch_k
        self.stoch_d           = stoch_d
        self.adx_period        = adx_period
        self.adx_threshold     = adx_threshold
        self.min_histogram     = min_histogram
        self.min_confirmations = min_confirmations
        self.bb_period         = bb_period
        self.bb_std            = bb_std
        self.cooldown_bars     = cooldown_bars
        self._last_signal_bar: int = -999   # bar index when last signal fired

        self.opens:      list[float] = []
        self.closes:     list[float] = []
        self.highs:      list[float] = []
        self.lows:       list[float] = []
        self.volumes:    list[float] = []
        self.open_times: list[int]   = []

    def seed(self, historical: list[Kline]) -> None:
        """Pre-load historical bars so indicators are ready immediately."""
        for k in historical:
            self.opens.append(k.open)
            self.closes.append(k.close)
            self.highs.append(k.high)
            self.lows.append(k.low)
            self.volumes.append(k.volume)
            self.open_times.append(k.open_time)

    def update(self, kline: Kline) -> Optional[Signal]:
        """Ingest a closed candle and return a Signal if one is detected."""
        self.opens.append(kline.open)
        self.closes.append(kline.close)
        self.highs.append(kline.high)
        self.lows.append(kline.low)
        self.volumes.append(kline.volume)
        self.open_times.append(kline.open_time)
        return self._detect()

    def history_snapshots(self) -> list[IndicatorSnapshot]:
        """Compute indicator snapshots for all seeded bars (O(n), used by UI on startup)."""
        closes = self.closes
        highs  = self.highs
        lows   = self.lows
        n      = len(closes)

        ema200_line = ema(closes, self.ema_slow)
        ema50_line  = ema(closes, self.ema_fast)
        macd_res    = macd(closes, fast_period=self.macd_fast,
                           slow_period=self.macd_slow, signal_period=self.macd_signal)
        rsi_line    = rsi(closes, period=self.rsi_period)
        adx_res     = adx(highs, lows, closes, period=self.adx_period)
        bb_res      = bollinger_bands(closes)

        result = []
        for i in range(n):
            ml = macd_res.macd_line[i]
            ms = macd_res.signal_line[i]
            result.append(IndicatorSnapshot(
                time=self.open_times[i] // 1000,
                open=self.opens[i],
                high=highs[i],
                low=lows[i],
                close=closes[i],
                volume=self.volumes[i],
                ema50=ema50_line[i],
                ema200=ema200_line[i],
                macd_line=ml,
                macd_signal=ms,
                macd_hist=(ml - ms) if ml is not None and ms is not None else None,
                rsi_val=rsi_line[i],
                adx_val=adx_res.adx[i],
                bb_upper=bb_res.upper[i],
                bb_middle=bb_res.middle[i],
                bb_lower=bb_res.lower[i],
            ))
        return result

    def current_snapshot(self) -> IndicatorSnapshot:
        """Return the indicator snapshot for the most recent bar."""
        return self.history_snapshots()[-1]

    # ── Private ───────────────────────────────────────────────────────────────

    def _detect(self) -> Optional[Signal]:
        closes  = self.closes
        highs   = self.highs
        lows    = self.lows
        volumes = self.volumes
        n       = len(closes)
        price   = closes[-1]

        # 1. EMA trend gates
        ema200_line = ema(closes, self.ema_slow)
        ema50_line  = ema(closes, self.ema_fast)
        ema200 = ema200_line[n - 1]
        ema50  = ema50_line[n - 1]

        if ema200 is None:
            return None

        above_ema200 = price > ema200
        above_ema50  = ema50 is not None and price > ema50

        # 2. ADX trend strength gate
        adx_res = adx(highs, lows, closes, period=self.adx_period)
        adx_val = adx_res.adx[n - 1]

        if adx_val is None or adx_val < self.adx_threshold:
            return None  # market too choppy or no defined ADX yet

        # 3. MACD crossover trigger
        macd_res  = macd(closes, fast_period=self.macd_fast, slow_period=self.macd_slow, signal_period=self.macd_signal)
        macd_prev = macd_res.macd_line[n - 2]
        macd_curr = macd_res.macd_line[n - 1]
        sig_prev  = macd_res.signal_line[n - 2]
        sig_curr  = macd_res.signal_line[n - 1]

        if any(v is None for v in (macd_prev, macd_curr, sig_prev, sig_curr)):
            return None

        cross_up   = macd_prev <= sig_prev and macd_curr > sig_curr
        cross_down = macd_prev >= sig_prev and macd_curr < sig_curr

        if not cross_up and not cross_down:
            return None

        # Reject hairline crossovers — MACD must have meaningful separation
        histogram = macd_curr - sig_curr
        if abs(histogram) < self.min_histogram:
            return None

        # Block signals that contradict EMA(200) trend direction
        if cross_up   and not above_ema200:
            return None
        if cross_down and above_ema200:
            return None

        # 4. Bollinger Band position gate — prevents buying at overextended prices.
        #    BUY only when price is below the BB midline (lower half = room to rise).
        #    SELL only when price is above the BB midline (upper half = room to fall).
        bb_res = bollinger_bands(closes, period=self.bb_period, std_dev=self.bb_std)
        bb_mid = bb_res.middle[n - 1]
        if bb_mid is not None:
            if cross_up   and price > bb_mid:
                return None   # price already in upper band — overextended for a buy
            if cross_down and price < bb_mid:
                return None   # price already in lower band — overextended for a sell

        # 5. Cooldown gate — prevents rapid-fire signals after a recent one
        if n - self._last_signal_bar < self.cooldown_bars:
            return None

        # 6–8. Confirmations
        rsi_line = rsi(closes, period=self.rsi_period)
        rsi_curr = rsi_line[n - 1]

        stoch_res    = stochastic(highs, lows, closes, k_period=self.stoch_k, d_period=self.stoch_d)
        stoch_k_prev = stoch_res.k[n - 2]
        stoch_k_curr = stoch_res.k[n - 1]
        stoch_d_prev = stoch_res.d[n - 2]
        stoch_d_curr = stoch_res.d[n - 1]

        obv_line   = obv(closes, volumes)
        obv_rising = obv_line[n - 1] > obv_line[n - 2]

        # Trend summary line
        ema_desc = (
            "above both" if above_ema50 and above_ema200 else
            "below both" if not above_ema50 and not above_ema200 else
            "mixed"
        )
        trend_note = (
            f"EMA50={ema50:.2f}  EMA200={ema200:.2f} ({ema_desc})  ADX={adx_val:.1f}"
            if ema50 is not None
            else f"EMA200={ema200:.2f}  ADX={adx_val:.1f}"
        )

        if cross_up:
            # Stochastic zone: only count the cross if %K was coming from a low zone (< 50)
            # — prevents treating overbought momentum as a fresh buy entry
            stoch_buy_ok = (
                _stoch_cross_up(stoch_k_prev, stoch_d_prev, stoch_k_curr, stoch_d_curr)
                and stoch_k_prev is not None and stoch_k_prev < 50
            )
            sig = self._build_signal(
                direction="BUY",
                price=price, macd_curr=macd_curr, sig_curr=sig_curr,
                histogram=histogram, adx_val=adx_val, trend_note=trend_note,
                confirmations=[
                    # RSI: healthy buy zone — not overbought, has upward momentum
                    (rsi_curr is not None and 30 <= rsi_curr <= 65,
                     f"RSI={rsi_curr:.1f} (healthy buy zone)" if rsi_curr else ""),
                    (stoch_buy_ok,
                     f"Stoch %K crossed above %D from low zone ({stoch_k_curr:.1f}/{stoch_d_curr:.1f})"
                     if stoch_k_curr is not None and stoch_d_curr is not None else ""),
                    (obv_rising, "OBV rising"),
                ],
            )
        else:
            # Stochastic zone: only count the cross if %K was coming from a high zone (> 50)
            stoch_sell_ok = (
                _stoch_cross_down(stoch_k_prev, stoch_d_prev, stoch_k_curr, stoch_d_curr)
                and stoch_k_prev is not None and stoch_k_prev > 50
            )
            sig = self._build_signal(
                direction="SELL",
                price=price, macd_curr=macd_curr, sig_curr=sig_curr,
                histogram=histogram, adx_val=adx_val, trend_note=trend_note,
                confirmations=[
                    # RSI: healthy sell zone — not oversold, has downward momentum
                    (rsi_curr is not None and 35 <= rsi_curr <= 70,
                     f"RSI={rsi_curr:.1f} (healthy sell zone)" if rsi_curr else ""),
                    (stoch_sell_ok,
                     f"Stoch %K crossed below %D from high zone ({stoch_k_curr:.1f}/{stoch_d_curr:.1f})"
                     if stoch_k_curr is not None and stoch_d_curr is not None else ""),
                    (not obv_rising, "OBV falling"),
                ],
            )

        # 9. Minimum confirmations gate — suppress LOW signals
        if len(sig.reasons) < self.min_confirmations:
            return None

        self._last_signal_bar = n   # record cooldown checkpoint
        return sig

    def _build_signal(
        self,
        direction: str,
        price: float,
        macd_curr: float,
        sig_curr: float,
        histogram: float,
        adx_val: float,
        trend_note: str,
        confirmations: list[tuple[bool, str]],
    ) -> Signal:
        reasons = [msg for met, msg in confirmations if met and msg]
        return Signal(
            direction=direction,
            confidence=_confidence(len(reasons)),
            entry_price=price,
            open_time=self.open_times[-1],
            macd_val=macd_curr,
            signal_val=sig_curr,
            histogram=histogram,
            adx_val=adx_val,
            trend_note=trend_note,
            reasons=reasons,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stoch_cross_up(k_prev, d_prev, k_curr, d_curr) -> bool:
    if any(v is None for v in (k_prev, d_prev, k_curr, d_curr)):
        return False
    return k_prev <= d_prev and k_curr > d_curr


def _stoch_cross_down(k_prev, d_prev, k_curr, d_curr) -> bool:
    if any(v is None for v in (k_prev, d_prev, k_curr, d_curr)):
        return False
    return k_prev >= d_prev and k_curr < d_curr


def _confidence(count: int) -> str:
    if count == 3:
        return "HIGH"
    if count == 2:
        return "MEDIUM"
    return "LOW"
