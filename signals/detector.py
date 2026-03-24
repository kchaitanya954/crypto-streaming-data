"""
Signal detector: combines trend gates and entry indicators to produce buy/sell signals.

Signal flow:
  1. EMA(200) hard gate      — blocks signals against the long-term trend
  2. ADX gate                — blocks signals when market is choppy (ADX < threshold)
  3. MACD crossover          — trigger, must exceed min_histogram gap to avoid micro-crosses
  4. RSI                     — confirmation (+1 if not overbought/oversold)
  5. Stochastic              — confirmation (+1 if %K/%D cross agrees)
  6. OBV                     — confirmation (+1 if volume backs the move)
  7. min_confirmations gate  — signal is suppressed if too few confirmations pass
"""

from dataclasses import dataclass, field
from typing import Optional

from indicators import macd, rsi, stochastic, obv, ema, adx
from streaming.stream import Kline


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
ADX_THRESHOLD     = 30.0   # raised from 25 — requires a clearer trend
MIN_HISTOGRAM     = 0.5    # min |MACD - signal| to count as a real crossover (tune per asset)
MIN_CONFIRMATIONS = 2      # require at least MEDIUM confidence — blocks LOW signals


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

        self.closes:     list[float] = []
        self.highs:      list[float] = []
        self.lows:       list[float] = []
        self.volumes:    list[float] = []
        self.open_times: list[int]   = []

    def seed(self, historical: list[Kline]) -> None:
        """Pre-load historical bars so indicators are ready immediately."""
        for k in historical:
            self.closes.append(k.close)
            self.highs.append(k.high)
            self.lows.append(k.low)
            self.volumes.append(k.volume)
            self.open_times.append(k.open_time)

    def update(self, kline: Kline) -> Optional[Signal]:
        """Ingest a closed candle and return a Signal if one is detected."""
        self.closes.append(kline.close)
        self.highs.append(kline.high)
        self.lows.append(kline.low)
        self.volumes.append(kline.volume)
        self.open_times.append(kline.open_time)
        return self._detect()

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

        # 4–6. Confirmations
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
            sig = self._build_signal(
                direction="BUY",
                price=price, macd_curr=macd_curr, sig_curr=sig_curr,
                histogram=histogram, adx_val=adx_val, trend_note=trend_note,
                confirmations=[
                    (rsi_curr is not None and rsi_curr < 60,
                     f"RSI={rsi_curr:.1f} (not overbought)" if rsi_curr else ""),
                    (_stoch_cross_up(stoch_k_prev, stoch_d_prev, stoch_k_curr, stoch_d_curr),
                     f"Stoch %K crossed above %D ({stoch_k_curr:.1f}/{stoch_d_curr:.1f})"
                     if stoch_k_curr is not None and stoch_d_curr is not None else ""),
                    (obv_rising, "OBV rising"),
                ],
            )
        else:
            sig = self._build_signal(
                direction="SELL",
                price=price, macd_curr=macd_curr, sig_curr=sig_curr,
                histogram=histogram, adx_val=adx_val, trend_note=trend_note,
                confirmations=[
                    (rsi_curr is not None and rsi_curr > 40,
                     f"RSI={rsi_curr:.1f} (not oversold)" if rsi_curr else ""),
                    (_stoch_cross_down(stoch_k_prev, stoch_d_prev, stoch_k_curr, stoch_d_curr),
                     f"Stoch %K crossed below %D ({stoch_k_curr:.1f}/{stoch_d_curr:.1f})"
                     if stoch_k_curr is not None and stoch_d_curr is not None else ""),
                    (not obv_rising, "OBV falling"),
                ],
            )

        # 7. Minimum confirmations gate — suppress LOW signals
        if len(sig.reasons) < self.min_confirmations:
            return None

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
