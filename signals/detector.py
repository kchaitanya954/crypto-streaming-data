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
ADX_THRESHOLD     = 30.0   # below this = choppy market, skip signal
MIN_HISTOGRAM     = 1.0    # min |MACD − signal| to reject micro-crossovers
MIN_CONFIRMATIONS = 2      # require MEDIUM or HIGH — LOW signals always suppressed
BB_PERIOD         = 20     # Bollinger Band lookback
BB_STD            = 2.0    # Bollinger Band standard deviation multiplier
COOLDOWN_BARS     = 3      # bars to wait after a signal before allowing the next one


# ── Adaptive params by timeframe ──────────────────────────────────────────────

def params_for_interval(interval: str) -> dict:
    """
    Return SignalDetector kwargs tuned for the given Binance interval string.

    Four tiers:
      Scalping  (< 3 min/bar):  1s, 2s, 1m, 2m  — fast MACD, low ADX, tiny histogram
      Intraday  (3–59 min/bar): 3m … 30m         — medium settings
      Swing     (1–5 h/bar):    1h … 4h           — standard settings
      Position  (≥ 6 h/bar):    6h, 12h, 1d, …   — slower settings, higher histogram
    """
    unit = interval[-1]                       # s / m / h / d / w / M
    try:
        n = int(interval[:-1])
    except ValueError:
        n = 1

    to_min = {'s': 1/60, 'm': 1, 'h': 60, 'd': 1440, 'w': 10080, 'M': 43200}
    minutes = n * to_min.get(unit, 1)

    if minutes < 3:          # ── Scalping (1s, 2s, 1m, 2m) ──
        return dict(
            macd_fast=3,    macd_slow=10,  macd_signal=3,
            ema_fast=9,     ema_slow=50,
            rsi_period=7,   stoch_k=5,     stoch_d=3,
            adx_period=7,   adx_threshold=12.0,
            min_histogram=0.0,
            min_confirmations=1, cooldown_bars=2,
            take_profit_pct=0.8,  stop_loss_pct=0.4,
        )
    elif minutes < 60:       # ── Intraday (3m – 30m) ──
        return dict(
            macd_fast=6,    macd_slow=14,  macd_signal=5,
            ema_fast=21,    ema_slow=55,
            rsi_period=10,  stoch_k=9,     stoch_d=3,
            adx_period=10,  adx_threshold=18.0,
            min_histogram=0.02,
            min_confirmations=1, cooldown_bars=3,
            take_profit_pct=2.0,  stop_loss_pct=1.0,
        )
    elif minutes < 360:      # ── Swing (1h – 4h) ──
        return dict(
            macd_fast=12,   macd_slow=26,  macd_signal=9,
            ema_fast=50,    ema_slow=200,
            rsi_period=14,  stoch_k=14,    stoch_d=3,
            adx_period=14,  adx_threshold=20.0,
            min_histogram=0.1,
            min_confirmations=1, cooldown_bars=5,
            take_profit_pct=4.0,  stop_loss_pct=2.0,
        )
    else:                    # ── Position (6h, 12h, 1d, 1w …) ──
        return dict(
            macd_fast=12,   macd_slow=26,  macd_signal=9,
            ema_fast=50,    ema_slow=200,
            rsi_period=14,  stoch_k=14,    stoch_d=3,
            adx_period=14,  adx_threshold=22.0,
            min_histogram=1.0,
            min_confirmations=1, cooldown_bars=3,
            take_profit_pct=8.0,  stop_loss_pct=4.0,
        )


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
      - min_histogram (default 1.0): skip hairline MACD crossovers
      - min_confirmations (default 2): require MEDIUM (2) or HIGH (3) confidence
      - cooldown_bars (default 3): min candles between signals to prevent whipsaw

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
        take_profit_pct:   float = 3.0,
        stop_loss_pct:     float = 1.5,
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
        self.take_profit_pct   = take_profit_pct
        self.stop_loss_pct     = stop_loss_pct
        self._bar_count:       int = 0
        self._last_signal_bar: int = -999   # bar index when last signal fired
        # Position tracking — prevents consecutive same-direction signals
        self._open_position:   Optional[str]   = None   # "LONG", "SHORT", or None
        self._entry_price:     Optional[float] = None   # price at which position was entered

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
        self._bar_count += 1
        self.opens.append(kline.open)
        self.closes.append(kline.close)
        self.highs.append(kline.high)
        self.lows.append(kline.low)
        self.volumes.append(kline.volume)
        self.open_times.append(kline.open_time)
        signal = self._detect()
        if signal:
            self._last_signal_bar = self._bar_count
            # Update position state so the next signal must be in the opposite direction
            if signal.direction == "BUY":
                if self._open_position is None:
                    self._open_position = "LONG"
                    self._entry_price = signal.entry_price
                elif self._open_position == "SHORT":
                    self._open_position = None
                    self._entry_price = None
            else:  # SELL
                if self._open_position is None:
                    self._open_position = "SHORT"
                    self._entry_price = signal.entry_price
                elif self._open_position == "LONG":
                    self._open_position = None
                    self._entry_price = None
        return signal

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

    def _check_position_exit(self) -> Optional[Signal]:
        """
        When in an open position, check for exit conditions:
          1. Take-profit: price moved >= take_profit_pct in our favour
          2. Stop-loss:   price moved >= stop_loss_pct against us
          3. Technical:   MACD crosses in the exit direction

        Exit signals use relaxed criteria — no EMA or ADX filter needed
        because we're closing an existing position, not opening a new one.
        """
        if self._open_position is None or self._entry_price is None:
            return None

        closes = self.closes
        highs  = self.highs
        lows   = self.lows
        n      = len(closes)
        price  = closes[-1]

        if n < 2:
            return None

        pct_change = (price - self._entry_price) / self._entry_price * 100
        is_long    = self._open_position == "LONG"
        exit_reason: Optional[str] = None

        # --- Price-based exits ---
        if is_long:
            profit_pct = pct_change
        else:
            profit_pct = -pct_change   # short profits when price falls

        if profit_pct >= self.take_profit_pct:
            exit_reason = f"Take-profit +{profit_pct:.2f}% (target {self.take_profit_pct}%)"
        elif profit_pct <= -self.stop_loss_pct:
            exit_reason = f"Stop-loss {profit_pct:.2f}% (limit -{self.stop_loss_pct}%)"

        # --- MACD technical exit (in addition to or instead of price exits) ---
        macd_res  = macd(closes, fast_period=self.macd_fast,
                         slow_period=self.macd_slow, signal_period=self.macd_signal)
        macd_prev = macd_res.macd_line[n - 2]
        macd_curr = macd_res.macd_line[n - 1]
        sig_prev  = macd_res.signal_line[n - 2]
        sig_curr  = macd_res.signal_line[n - 1]

        if all(v is not None for v in (macd_prev, macd_curr, sig_prev, sig_curr)):
            if is_long and macd_prev >= sig_prev and macd_curr < sig_curr:
                if exit_reason is None:
                    exit_reason = "MACD cross-down: closing long"
            elif not is_long and macd_prev <= sig_prev and macd_curr > sig_curr:
                if exit_reason is None:
                    exit_reason = "MACD cross-up: closing short"

        if exit_reason is None:
            return None

        # Build the closing signal
        adx_res = adx(highs, lows, closes, period=self.adx_period)
        adx_val = adx_res.adx[n - 1] or 0.0
        hist    = (macd_curr - sig_curr) if (macd_curr is not None and sig_curr is not None) else 0.0
        direction = "SELL" if is_long else "BUY"

        return self._build_signal(
            direction=direction,
            price=price,
            macd_curr=macd_curr or 0.0,
            sig_curr=sig_curr or 0.0,
            histogram=hist,
            adx_val=adx_val,
            trend_note=(
                f"Position exit · entry={self._entry_price:.4f} · "
                f"change={pct_change:+.2f}%"
            ),
            confirmations=[
                (True, exit_reason),
                (True, f"Position mgmt ({self._open_position})"),
            ],
        )

    def _detect(self) -> Optional[Signal]:
        # If in an open position, only look for exit signals
        if self._open_position is not None:
            if self._bar_count - self._last_signal_bar < self.cooldown_bars:
                return None
            return self._check_position_exit()

        # Cooldown gate — prevent whipsaw signals on consecutive candles
        if self._bar_count - self._last_signal_bar < self.cooldown_bars:
            return None

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

        # Block signals that contradict the trend direction
        # BUY: price must be above EMA200 (long-term uptrend)
        if cross_up and not above_ema200:
            return None
        # SELL: price must be below both EMA200 AND EMA50 — no selling into any uptrend
        if cross_down and (above_ema200 or above_ema50):
            return None

        # 4. Cooldown gate — prevents rapid-fire signals after a recent one
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
    return k_prev < d_prev and k_curr > d_curr


def _stoch_cross_down(k_prev, d_prev, k_curr, d_curr) -> bool:
    if any(v is None for v in (k_prev, d_prev, k_curr, d_curr)):
        return False
    return k_prev > d_prev and k_curr < d_curr


def _confidence(count: int) -> str:
    if count == 3:
        return "HIGH"
    if count == 2:
        return "MEDIUM"
    return "LOW"
