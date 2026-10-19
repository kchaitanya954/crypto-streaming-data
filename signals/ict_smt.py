"""
ICT / SMT-divergence session strategy — crypto adaptation of the NY-open routine.

The routine, and how each step is implemented here:

  1. Before 09:30 New York, prepare           → session levels and bias are frozen at
                                                 the kill-zone start (09:30 NY, DST-aware)
  2. 1h/4h chart: decide rise or fall         → market_structure() on 1h (BOS/CHoCH with
                                                 fractal swings); optional 4h agreement
  3. 1m/5m chart: precise setup               → LTF bars (default 5m) built from 1m bars
  4. S&P 500 vs Nasdaq SMT divergence         → BTC vs ETH (CoinDCX has no index futures):
                                                 one asset sweeps a session level, the
                                                 other fails to — the classic SMT
  5. Fair Value Gap after a strong move       → displacement candle (body ≥ k×ATR) that
                                                 leaves a 3-bar FVG and breaks structure
  6. Wait for the retrace, enter near it      → resting limit at the FVG midpoint
                                                 (consequent encroachment) → maker fee
  7. SL 1R, target 2R–3R at a liquidity level → SL beyond the sweep extreme; TP = nearest
                                                 untaken opposing liquidity ≥ min_rr
                                                 (capped at max_rr, else min_rr fallback)
  8. Exit / manage                            → SL/TP on the exchange, time stop before the
                                                 NY close, one filled trade per day

The engine is pure: it consumes aligned closed 1m bars for the traded (primary)
and the reference asset and emits events. The backtester, paper mode and live
mode all drive the same engine, so what is tested is what trades.
"""

from __future__ import annotations

import bisect
import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
_UTC = _dt.timezone.utc


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    t: int        # open time, ms since epoch (UTC)
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass
class ICTConfig:
    ltf_minutes:        int   = 5           # setup timeframe (routine: 1m or 5m)
    htf_minutes:        tuple = (60,)       # bias timeframes that must agree (60 and/or 240)
    htf_swing_lb:       int   = 2           # fractal bars each side for HTF swings
    ltf_swing_lb:       int   = 2           # fractal bars each side for LTF swings
    kz_start:           str   = "09:30"     # NY time — first bar that may set up
    kz_end:             str   = "11:00"     # NY time — no new setups after this
    order_expiry_min:   int   = 30          # resting order lifetime
    order_cutoff:       str   = "11:30"     # NY time — every resting order is gone by then
    time_stop:          str   = "15:55"     # NY time — flatten anything still open
    weekdays_only:      bool  = True        # the routine is built on the NYSE open
    require_smt:        bool  = True
    entry_mode:         str   = "ce"        # "ce" = FVG midpoint, "edge" = near edge
    displacement_atr:   float = 1.0         # displacement body ≥ k × ATR(14, LTF)
    sl_buffer_atr:      float = 0.1         # SL placed this many ATRs beyond the extreme
    min_rr:             float = 2.0
    max_rr:             float = 4.0
    min_risk_pct:       float = 0.20        # tighter stops let fees eat > ~40% of 1R
    max_risk_pct:       float = 1.50
    max_setups_per_day: int   = 2           # a second setup only if the first never filled
    max_trades_per_day: int   = 1
    tp_swing_lookback:  int   = 48          # LTF bars of swing highs/lows used as targets
    mss_lookback:       int   = 24          # LTF bars searched for the swing that MSS breaks


@dataclass
class Setup:
    direction:   str            # "long" | "short"
    entry:       float
    sl:          float
    tp:          float
    rr:          float
    risk_pct:    float          # |entry − sl| as % of entry
    created_ms:  int            # close time of the LTF bar that completed the setup
    expiry_ms:   int
    tp_source:   str
    smt_level:   str            # which session level produced the SMT
    smt_swept_by: str           # "primary" | "reference"
    bias:        int
    fvg_low:     float
    fvg_high:    float
    reasons:     list[str] = field(default_factory=list)


@dataclass
class Event:
    kind: str                   # "plan" | "setup" | "no_trade"
    t_ms: int
    setup: Optional[Setup] = None
    info: dict = field(default_factory=dict)


# ── Small helpers ─────────────────────────────────────────────────────────────

def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def ny_minute_of_day(t_ms: int) -> tuple[_dt.date, int]:
    """(NY calendar date, minute of day) for a UTC ms timestamp."""
    d = _dt.datetime.fromtimestamp(t_ms / 1000, _UTC).astimezone(NY)
    return d.date(), d.hour * 60 + d.minute


def ny_time_to_ms(day: _dt.date, minute_of_day: int) -> int:
    d = _dt.datetime(day.year, day.month, day.day, tzinfo=NY) + _dt.timedelta(minutes=minute_of_day)
    return int(d.timestamp() * 1000)


def wilder_atr(bars: list[Bar], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        b, pc = bars[i], bars[i - 1].c
        trs.append(max(b.h - b.l, abs(b.h - pc), abs(b.l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def fractal_swings(bars: list[Bar], lb: int) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    Confirmed fractal swing highs and lows as (index, price). A swing at i needs
    `lb` bars on each side, so it is only known lb bars later — no lookahead.
    """
    highs, lows = [], []
    for i in range(lb, len(bars) - lb):
        h, l = bars[i].h, bars[i].l
        if all(h > bars[k].h for k in range(i - lb, i)) and all(h >= bars[k].h for k in range(i + 1, i + lb + 1)):
            highs.append((i, h))
        if all(l < bars[k].l for k in range(i - lb, i)) and all(l <= bars[k].l for k in range(i + 1, i + lb + 1)):
            lows.append((i, l))
    return highs, lows


def market_structure(bars: list[Bar], lb: int = 2) -> int:
    """
    +1 bullish / −1 bearish / 0 undefined, from break-of-structure logic: a close
    above the last confirmed swing high flips bullish, a close below the last
    confirmed swing low flips bearish. Swings confirm lb bars after they print.
    """
    last_sh: Optional[float] = None
    last_sl: Optional[float] = None
    trend = 0
    for i in range(len(bars)):
        j = i - lb
        if j >= lb:
            hj, lj = bars[j].h, bars[j].l
            if all(hj > bars[k].h for k in range(j - lb, j)) and all(hj >= bars[k].h for k in range(j + 1, i + 1)):
                last_sh = hj
            if all(lj < bars[k].l for k in range(j - lb, j)) and all(lj <= bars[k].l for k in range(j + 1, i + 1)):
                last_sl = lj
        c = bars[i].c
        if last_sh is not None and c > last_sh:
            trend, last_sh = 1, None
        elif last_sl is not None and c < last_sl:
            trend, last_sl = -1, None
    return trend


class _Agg:
    """Builds N-minute bars (UTC-aligned) from closed 1m bars."""

    def __init__(self, minutes: int, keep: int) -> None:
        self.minutes = minutes
        self.keep = keep
        self.bars: list[Bar] = []
        self._cur: Optional[Bar] = None

    def add(self, b: Bar) -> Optional[Bar]:
        bucket = (b.t // 60000) // self.minutes * self.minutes * 60000
        if self._cur is not None and self._cur.t != bucket:
            self._push(self._cur)        # a data gap skipped the bucket's last minute
            self._cur = None
        if self._cur is None:
            self._cur = Bar(bucket, b.o, b.h, b.l, b.c, b.v)
        else:
            c = self._cur
            c.h, c.l, c.c, c.v = max(c.h, b.h), min(c.l, b.l), b.c, c.v + b.v
        if ((b.t // 60000) + 1) % self.minutes == 0:   # last minute of the bucket closed
            done, self._cur = self._cur, None
            self._push(done)
            return done
        return None

    def _push(self, bar: Bar) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.keep:
            del self.bars[: len(self.bars) - self.keep]


# ── Engine ────────────────────────────────────────────────────────────────────

_LOW_LEVELS  = ("asia_low", "london_low", "premarket_low", "pdl")
_HIGH_LEVELS = ("asia_high", "london_high", "premarket_high", "pdh")


class ICTEngine:
    """
    Feed aligned closed 1m bars with on_minute(primary, reference). Returns a list
    of Events. The driver owns order handling and reports back through
    order_finished(filled) so the per-day trade limits hold.
    """

    def __init__(self, cfg: Optional[ICTConfig] = None) -> None:
        self.cfg = cfg or ICTConfig()
        c = self.cfg
        self._kz_start, self._kz_end = _hm(c.kz_start), _hm(c.kz_end)
        self._cutoff = _hm(c.order_cutoff)
        self._ltf_p, self._ltf_r = _Agg(c.ltf_minutes, 600), _Agg(c.ltf_minutes, 600)
        self._htf = {m: _Agg(m, 400) for m in c.htf_minutes}
        self._day: Optional[_dt.date] = None
        # (NY minute-of-day, 1m bar) since 00:00 NY, for both assets
        self._today: dict[str, list[tuple[int, Bar]]] = {"p": [], "r": []}
        self._yesterday: dict[str, list[tuple[int, Bar]]] = {"p": [], "r": []}
        self._reset_day_state()

    # ── public ────────────────────────────────────────────────────────────────

    def on_minute(self, p: Bar, r: Bar) -> list[Event]:
        events: list[Event] = []
        day, mod = ny_minute_of_day(p.t)
        if day != self._day:
            if self._day is not None:
                self._yesterday = self._today
            self._today = {"p": [], "r": []}
            self._day = day
            self._reset_day_state()
        self._today["p"].append((mod, p))
        self._today["r"].append((mod, r))
        for agg in self._htf.values():
            agg.add(p)
        lp, lr = self._ltf_p.add(p), self._ltf_r.add(r)

        if self.cfg.weekdays_only and day.weekday() >= 5:
            return events

        bar_close_mod = mod + 1          # minute-of-day at which this 1m bar closed
        if self._plan is None and bar_close_mod >= self._kz_start and mod < self._kz_end:
            events.append(self._make_plan(p.t + 60000))
        if lp is None or lr is None or self._plan is None or self._plan.get("skip"):
            return events

        if self._state == "idle" and bar_close_mod <= self._kz_end:
            ev = self._evaluate(p.t + 60000)
            if ev is not None:
                events.append(ev)
        elif self._state == "idle" and not self._reported_no_trade and bar_close_mod > self._kz_end:
            self._reported_no_trade = True
            self._state = "done"
            events.append(Event("no_trade", p.t + 60000, info={"reason": self._last_reject or "no SMT + FVG setup in the kill zone"}))
        return events

    def mark_traded_today(self, n: int = 1) -> None:
        """Live restart: today's trades already happened (from the DB) — respect the limits."""
        self._trades = max(self._trades, n)
        self._setups = max(self._setups, n)
        self._state = "done" if self._trades >= self.cfg.max_trades_per_day else self._state

    def hold(self) -> None:
        """Live restart: an order/position from earlier today is still active — no new setups."""
        self._state = "pending"

    def order_finished(self, filled: bool) -> None:
        """Driver callback: a resting order was filled (trade done) or cancelled/expired."""
        if filled:
            self._trades += 1
        self._state = "done" if self._trades >= self.cfg.max_trades_per_day or \
            self._setups >= self.cfg.max_setups_per_day else "idle"

    # ── day state ─────────────────────────────────────────────────────────────

    def _reset_day_state(self) -> None:
        self._plan: Optional[dict] = None
        self._state = "idle"             # idle → pending (driver holds an order) → idle/done
        self._setups = 0
        self._trades = 0
        self._kz_start_ms: Optional[int] = None
        self._reported_no_trade = False
        self._last_reject: Optional[str] = None

    def _make_plan(self, t_ms: int) -> Event:
        """Freeze bias and liquidity levels at the kill-zone start."""
        c = self.cfg
        biases = {m: market_structure(agg.bars[-200:], c.htf_swing_lb) for m, agg in self._htf.items()}
        vals = set(biases.values())
        bias = vals.pop() if len(vals) == 1 else 0
        levels = {"p": self._levels("p"), "r": self._levels("r")}
        self._kz_start_ms = t_ms
        self._plan = {"bias": bias, "htf": biases, "levels": levels, "skip": bias == 0}
        info = {"bias": bias, "htf": biases, "levels": levels}
        if bias == 0:
            self._state = "done"
            info["reason"] = "HTF structure undefined or timeframes disagree — no trade today"
        return Event("plan", t_ms, info=info)

    def _levels(self, who: str) -> dict:
        """Session liquidity levels for one asset, marking whether each is still untaken."""
        today, yday = self._today[who], self._yesterday[who]

        def rng(bars, a, b):
            sel = [x for m, x in bars if a <= m < b]
            return (min(x.l for x in sel), max(x.h for x in sel)) if sel else (None, None)

        out = {}
        asia_l, asia_h = rng(yday, _hm("20:00"), 24 * 60)
        lon_l, lon_h = rng(today, _hm("02:00"), _hm("05:00"))
        pre_l, pre_h = rng(today, _hm("08:00"), self._kz_start)
        pd_l = min((x.l for _, x in yday), default=None)
        pd_h = max((x.h for _, x in yday), default=None)
        after_asia = rng(today, 0, self._kz_start)
        after_lon = rng(today, _hm("05:00"), self._kz_start)

        def put(name, val, after, is_low):
            if val is None:
                return
            if after[0] is None:
                untaken = True
            else:
                untaken = after[0] > val if is_low else after[1] < val
            out[name] = {"price": val, "untaken": untaken}

        put("asia_low", asia_l, after_asia, True)
        put("asia_high", asia_h, after_asia, False)
        put("london_low", lon_l, after_lon, True)
        put("london_high", lon_h, after_lon, False)
        if pre_l is not None:   # ends at the kill-zone start, so untaken by construction
            out["premarket_low"] = {"price": pre_l, "untaken": True}
            out["premarket_high"] = {"price": pre_h, "untaken": True}
        put("pdl", pd_l, after_asia, True)
        put("pdh", pd_h, after_asia, False)
        return out

    # ── setup search ──────────────────────────────────────────────────────────

    def _reject(self, why: str) -> None:
        self._last_reject = why

    def _evaluate(self, t_ms: int) -> Optional[Event]:
        c, plan = self.cfg, self._plan
        bias = plan["bias"]
        P, R = self._ltf_p.bars, self._ltf_r.bars
        if self._kz_start_ms is None:
            return None
        # Anchor by time: the LTF buffer is trimmed, so stored indices go stale
        k0 = bisect.bisect_left(P, self._kz_start_ms, key=lambda b: b.t)
        if len(P) - k0 < 3 or len(P) < 20:
            return None
        kz_p, kz_r = P[k0:], R[k0:]
        lv_p, lv_r = plan["levels"]["p"], plan["levels"]["r"]
        long_ = bias > 0

        # 4. Sweep + SMT: the side we expect to be raided first is opposite the bias
        names = _LOW_LEVELS if long_ else _HIGH_LEVELS
        if long_:
            ext_i = min(range(len(kz_p)), key=lambda i: kz_p[i].l)
            ext_p, ext_r = kz_p[ext_i].l, min(b.l for b in kz_r)
            swept = lambda e, lvl: e < lvl
        else:
            ext_i = max(range(len(kz_p)), key=lambda i: kz_p[i].h)
            ext_p, ext_r = kz_p[ext_i].h, max(b.h for b in kz_r)
            swept = lambda e, lvl: e > lvl
        smt = None
        for n in names:
            a, b = lv_p.get(n), lv_r.get(n)
            if not a or not b or not (a["untaken"] and b["untaken"]):
                continue
            sp, sr = swept(ext_p, a["price"]), swept(ext_r, b["price"])
            if c.require_smt:
                if sp != sr:
                    smt = (n, "primary" if sp else "reference")
                    break
            elif sp or sr:
                smt = (n, "primary" if sp else "reference")
                break
        if smt is None:
            self._reject("no SMT divergence on any untaken session level")
            return None

        # 5. Displacement leaving an FVG, after the extreme, that breaks structure
        i = len(P) - 1
        a1, mid, a3 = P[i - 2], P[i - 1], P[i]
        abs_ext = k0 + ext_i
        if i - 2 < abs_ext:
            self._reject("waiting for displacement after the sweep extreme")
            return None
        atr = wilder_atr(P[-60:], 14)
        if not atr:
            return None
        body = (mid.c - mid.o) if long_ else (mid.o - mid.c)
        if body < c.displacement_atr * atr:
            self._reject("no displacement candle")
            return None
        if long_:
            if not a3.l > a1.h:
                self._reject("displacement left no fair value gap")
                return None
            fvg_low, fvg_high = a1.h, a3.l
        else:
            if not a3.h < a1.l:
                self._reject("displacement left no fair value gap")
                return None
            fvg_low, fvg_high = a3.h, a1.l

        lo = max(0, abs_ext - c.mss_lookback)
        sh, sl_ = fractal_swings(P[lo:abs_ext + 1], c.ltf_swing_lb)
        if long_:
            cand = [p for (j, p) in sh]
            mss = cand[-1] if cand else max(b.h for b in P[max(0, abs_ext - 6):abs_ext + 1])
            if max(mid.c, a3.c) <= mss:
                self._reject("no market structure shift")
                return None
        else:
            cand = [p for (j, p) in sl_]
            mss = cand[-1] if cand else min(b.l for b in P[max(0, abs_ext - 6):abs_ext + 1])
            if min(mid.c, a3.c) >= mss:
                self._reject("no market structure shift")
                return None

        # 6. Entry on the retrace into the gap
        if c.entry_mode == "edge":
            entry = fvg_high if long_ else fvg_low
        else:
            entry = (fvg_low + fvg_high) / 2

        # 7. Risk 1R beyond the sweep extreme, target opposing liquidity
        sl = ext_p - c.sl_buffer_atr * atr if long_ else ext_p + c.sl_buffer_atr * atr
        risk = (entry - sl) if long_ else (sl - entry)
        if risk <= 0:
            self._reject("invalid stop distance")
            return None
        risk_pct = risk / entry * 100
        if risk_pct < c.min_risk_pct or risk_pct > c.max_risk_pct:
            self._reject(f"stop distance {risk_pct:.2f}% outside {c.min_risk_pct}–{c.max_risk_pct}%")
            return None

        targets = self._targets(long_, entry, lv_p, P, k0)
        tp, src = None, "fallback"
        for name, price in targets:
            dist = (price - entry) if long_ else (entry - price)
            if dist >= c.min_rr * risk:
                tp, src = price, name
                break
        if tp is None:
            tp = entry + c.min_rr * risk if long_ else entry - c.min_rr * risk
        cap = entry + c.max_rr * risk if long_ else entry - c.max_rr * risk
        if (long_ and tp > cap) or (not long_ and tp < cap):
            tp, src = cap, f"{src} (capped {c.max_rr:g}R)"
        rr = abs(tp - entry) / risk

        day = self._day
        expiry = min(t_ms + c.order_expiry_min * 60000, ny_time_to_ms(day, self._cutoff))
        setup = Setup(
            direction="long" if long_ else "short",
            entry=entry, sl=sl, tp=tp, rr=rr, risk_pct=risk_pct,
            created_ms=t_ms, expiry_ms=expiry, tp_source=src,
            smt_level=smt[0], smt_swept_by=smt[1], bias=bias,
            fvg_low=fvg_low, fvg_high=fvg_high,
            reasons=[
                f"HTF bias {'bullish' if long_ else 'bearish'} ({', '.join(f'{m}m' for m in self._htf)})",
                f"SMT: {smt[1]} swept {smt[0].replace('_', ' ')}, the other asset held",
                f"displacement {body / atr:.1f}×ATR left FVG {fvg_low:.2f}–{fvg_high:.2f}",
                f"MSS through {mss:.2f}",
                f"target {src} at {tp:.2f} ({rr:.1f}R)",
            ],
        )
        self._state = "pending"
        self._setups += 1
        return Event("setup", t_ms, setup=setup)

    def _targets(self, long_: bool, entry: float, lv: dict, P: list[Bar], k0: int) -> list[tuple[str, float]]:
        """Untaken opposing liquidity beyond the entry, nearest first."""
        c = self.cfg
        names = _HIGH_LEVELS if long_ else _LOW_LEVELS
        kz = P[k0:]
        out = []
        for n in names:
            x = lv.get(n)
            if not x or not x["untaken"]:
                continue
            taken_in_kz = any(b.h > x["price"] for b in kz) if long_ else any(b.l < x["price"] for b in kz)
            if taken_in_kz:
                continue
            if (long_ and x["price"] > entry) or (not long_ and x["price"] < entry):
                out.append((n, x["price"]))
        window = P[-c.tp_swing_lookback:]
        sh, sl = fractal_swings(window, c.ltf_swing_lb)
        swings = sh if long_ else sl
        for j, price in swings:
            later = window[j + 1:]
            untaken = all(b.h <= price for b in later) if long_ else all(b.l >= price for b in later)
            if untaken and ((long_ and price > entry) or (not long_ and price < entry)):
                out.append(("ltf_swing_high" if long_ else "ltf_swing_low", price))
        out.sort(key=lambda x: abs(x[1] - entry))
        return out
