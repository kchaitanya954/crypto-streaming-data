"""
Tests for signals/ict_smt.py (engine) and signals/ict_sim.py (fill simulator).

The engine scenario is a scripted NY session: BTC sweeps the London low while ETH
holds its own (SMT), then a displacement candle leaves a fair value gap.
"""
import datetime as dt
import math

import pytest

import signals.ict_smt as ict
from signals.ict_smt import Bar, ICTConfig, ICTEngine, Setup, _Agg, fractal_swings, market_structure
from signals.ict_sim import SimCosts, SimTrade, step, summarize

NY = ict.NY
DAY = dt.date(2026, 10, 14)          # a Wednesday
ETH_K = 0.03                          # ETH = 3% of BTC except where they diverge


def _ny_ms(day: dt.date, hh: int, mm: int) -> int:
    return int(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY).timestamp() * 1000)


def _five(t0: int, o: float, h: float, l: float, c: float) -> list[Bar]:
    """Five 1m bars that aggregate to exactly (o, h, l, c)."""
    m = (o + c) / 2
    return [Bar(t0, o, h, min(o, c), m), Bar(t0 + 60000, m, max(o, c), l, m),
            Bar(t0 + 120000, m, m, m, m), Bar(t0 + 180000, m, m, m, m),
            Bar(t0 + 240000, m, max(o, c), min(o, c), c)]


def _session(eth_sweep_low: float) -> tuple[list[Bar], list[Bar]]:
    """1m bars from 19:00 NY the day before until 10:30 NY on DAY."""
    script: dict[tuple, tuple] = {
        (3, 0): (100000, 100010, 99900, 100000),          # London low 99,900
        (9, 40): (99950, 99960, 99700, 99880),            # BTC sweeps the London low
        (9, 45): (99880, 99900, 99870, 99890),            # a1
        (9, 50): (99890, 100160, 99885, 100150),          # displacement (mid)
        (9, 55): (100150, 100200, 100100, 100180),        # a3 → FVG 99,900–100,100
    }
    btc, eth = [], []
    start = _ny_ms(DAY - dt.timedelta(days=1), 19, 0)
    end = _ny_ms(DAY, 10, 30)
    i = 0
    for t in range(start, end, 300_000):
        d = dt.datetime.fromtimestamp(t / 1000, NY)
        key = (d.hour, d.minute) if d.date() == DAY else None
        if key in script:
            o, h, l, c = script[key]
        else:
            n0, n1 = 20 * math.sin(i), 20 * math.sin(i + 1)
            base = 99950 if (d.date() == DAY and (d.hour, d.minute) >= (9, 30)) else 100000
            o, c = base + n0, base + n1
            h, l = max(o, c) + 5, min(o, c) - 5
        i += 1
        bb = _five(t, o, h, l, c)
        eb = _five(t, o * ETH_K, h * ETH_K, l * ETH_K, c * ETH_K)
        if key in ((9, 40), (9, 45), (9, 50)):
            # ETH's own extreme is eth_sweep_low: it holds (or breaks) its London low on its own terms
            eo, eh, ec = (max(x * ETH_K, eth_sweep_low) for x in (o, h, c))
            eb = _five(t, eo, eh, eth_sweep_low if key == (9, 40) else max(l * ETH_K, eth_sweep_low), ec)
        btc += bb
        eth += eb
    return btc, eth


def _run(btc, eth, cfg=None):
    eng = ICTEngine(cfg or ICTConfig())
    events = []
    for p, r in zip(btc, eth):
        events += eng.on_minute(p, r)
    return events


@pytest.fixture
def bullish(monkeypatch):
    monkeypatch.setattr(ict, "market_structure", lambda bars, lb=2: 1)


# ── Engine ────────────────────────────────────────────────────────────────────

def test_smt_sweep_and_fvg_produce_long_setup(bullish):
    btc, eth = _session(eth_sweep_low=2998.0)            # ETH holds 2,997 London low
    events = _run(btc, eth)
    plans = [e for e in events if e.kind == "plan"]
    setups = [e.setup for e in events if e.kind == "setup"]
    assert plans and plans[0].info["bias"] == 1
    assert len(setups) == 1
    s = setups[0]
    assert s.direction == "long"
    assert s.smt_level == "london_low" and s.smt_swept_by == "primary"
    assert s.entry == pytest.approx(100000.0)            # FVG midpoint (consequent encroachment)
    assert s.sl < 99700                                  # beyond the sweep extreme
    assert s.tp == pytest.approx(s.entry + 2 * (s.entry - s.sl))   # no liquidity ≥ 2R → 2R fallback
    assert s.created_ms == _ny_ms(DAY, 10, 0)


def test_no_setup_without_smt_when_both_assets_sweep(bullish):
    btc, eth = _session(eth_sweep_low=2990.0)            # ETH also breaks its London low
    events = _run(btc, eth)
    assert not [e for e in events if e.kind == "setup"]


def test_same_day_without_smt_requirement_still_trades(bullish):
    btc, eth = _session(eth_sweep_low=2990.0)
    events = _run(btc, eth, ICTConfig(require_smt=False))
    assert [e for e in events if e.kind == "setup"]


def test_no_trade_when_bias_undefined(monkeypatch):
    monkeypatch.setattr(ict, "market_structure", lambda bars, lb=2: 0)
    events = _run(*_session(eth_sweep_low=2998.0))
    plan = next(e for e in events if e.kind == "plan")
    assert plan.info["bias"] == 0 and "no trade" in plan.info["reason"].lower()
    assert not [e for e in events if e.kind == "setup"]


def test_weekend_is_skipped(bullish):
    btc, eth = _session(eth_sweep_low=2998.0)
    shift = 3 * 86_400_000                                # Wednesday → Saturday
    btc = [Bar(b.t + shift, b.o, b.h, b.l, b.c) for b in btc]
    eth = [Bar(b.t + shift, b.o, b.h, b.l, b.c) for b in eth]
    assert _run(btc, eth) == []


def test_daily_limits_after_fill(bullish):
    eng = ICTEngine(ICTConfig())
    btc, eth = _session(eth_sweep_low=2998.0)
    setups = 0
    for p, r in zip(btc, eth):
        for e in eng.on_minute(p, r):
            if e.kind == "setup":
                setups += 1
                eng.order_finished(True)
    assert setups == 1 and eng._state == "done"


def test_mark_traded_today_blocks_new_setups(bullish):
    eng = ICTEngine(ICTConfig())
    btc, eth = _session(eth_sweep_low=2998.0)
    cut = next(i for i, b in enumerate(btc) if b.t >= _ny_ms(DAY, 9, 0))
    for p, r in zip(btc[:cut], eth[:cut]):
        eng.on_minute(p, r)
    eng.mark_traded_today(1)
    events = []
    for p, r in zip(btc[cut:], eth[cut:]):
        events += eng.on_minute(p, r)
    assert not [e for e in events if e.kind == "setup"]


# ── Building blocks ───────────────────────────────────────────────────────────

def _bars(closes):
    return [Bar(i * 3_600_000, c, c + 1, c - 1, c) for i, c in enumerate(closes)]


def test_market_structure_bullish_and_bearish():
    up = [100, 102, 101, 103, 105, 104, 106, 108, 107, 109, 111, 110, 112, 115]
    zig_up = _bars([100, 104, 102, 101, 103, 107, 105, 104, 106, 110, 108, 107, 109, 113])
    assert market_structure(zig_up, lb=1) == 1
    zig_down = _bars([113, 109, 111, 112, 110, 106, 108, 109, 107, 103, 105, 106, 104, 100])
    assert market_structure(zig_down, lb=1) == -1
    assert market_structure(_bars(up[:2]), lb=2) == 0


def test_fractal_swings_need_confirmation():
    bars = _bars([1, 2, 5, 2, 1])
    highs, lows = fractal_swings(bars, 2)
    assert highs == [(2, 6)]
    assert fractal_swings(bars[:4], 2)[0] == []          # right side not printed yet


def test_aggregator_builds_5m_bars_and_survives_gaps():
    agg = _Agg(5, 100)
    t0 = 1_800_000_000_000 // 300_000 * 300_000
    out = [agg.add(Bar(t0 + i * 60000, 10 + i, 11 + i, 9 + i, 10.5 + i)) for i in range(5)]
    assert out[:4] == [None] * 4 and out[4] is not None
    assert (out[4].o, out[4].h, out[4].l, out[4].c) == (10, 15, 9, 14.5)
    agg.add(Bar(t0 + 5 * 60000, 1, 2, 0, 1))             # bucket 2 starts, its last minute never arrives
    agg.add(Bar(t0 + 10 * 60000, 3, 4, 2, 3))            # bucket 3 → bucket 2 is flushed, not lost
    assert len(agg.bars) == 2 and agg.bars[1].t == t0 + 5 * 60000


def test_ny_session_follows_dst():
    # 09:30 NY is 13:30 UTC in October (EDT) and 14:30 UTC in December (EST)
    assert dt.datetime.fromtimestamp(_ny_ms(dt.date(2026, 10, 19), 9, 30) / 1000, dt.timezone.utc).hour == 13
    assert dt.datetime.fromtimestamp(_ny_ms(dt.date(2026, 12, 7), 9, 30) / 1000, dt.timezone.utc).hour == 14


# ── Simulator ─────────────────────────────────────────────────────────────────

def _setup(direction="long", entry=100.0, sl=99.0, tp=102.0, expiry=10_000_000):
    return Setup(direction=direction, entry=entry, sl=sl, tp=tp, rr=2.0, risk_pct=1.0,
                 created_ms=0, expiry_ms=expiry, tp_source="test", smt_level="london_low",
                 smt_swept_by="primary", bias=1, fvg_low=99.5, fvg_high=100.5)


def _bar(t, o, h, l, c):
    return Bar(t * 60000, o, h, l, c)


def test_touch_is_not_a_fill_trade_through_is():
    tr = SimTrade(_setup(), tick=0.1, time_stop_ms=10**13)
    assert step(tr, _bar(1, 100.5, 100.6, 100.0, 100.2), SimCosts()) is None   # touched, not through
    assert step(tr, _bar(2, 100.2, 100.3, 99.9, 100.1), SimCosts()) == "filled"


def test_stop_wins_when_bar_hits_both():
    tr = SimTrade(_setup(), tick=0.1, time_stop_ms=10**13)
    step(tr, _bar(1, 100.2, 100.3, 99.9, 100.1), SimCosts())
    assert step(tr, _bar(2, 100, 102.5, 98.9, 101), SimCosts()) == "closed"
    assert tr.exit_reason == "stop loss" and tr.exit_price == pytest.approx(99 * (1 - 0.0002))


def test_take_profit_r_multiple_net_of_fees():
    c = SimCosts()
    tr = SimTrade(_setup(), tick=0.1, time_stop_ms=10**13)
    step(tr, _bar(1, 100.2, 100.3, 99.9, 100.1), c)
    assert step(tr, _bar(2, 101, 102.1, 100.5, 102), c) == "closed"
    exit_px = 102 * (1 - c.tp_slip)
    expected = (exit_px - 100) - (100 * c.maker + exit_px * c.taker)
    assert tr.exit_reason == "take profit"
    assert tr.r_net(c) == pytest.approx(expected / 1.0)
    assert 1.85 < tr.r_net(c) < 2.0


def test_fill_and_stop_in_same_bar():
    tr = SimTrade(_setup(), tick=0.1, time_stop_ms=10**13)
    assert step(tr, _bar(1, 100.2, 100.3, 98.5, 99.0), SimCosts()) == "closed"
    assert tr.fill_ms == tr.exit_ms and tr.exit_reason == "stop loss"


def test_expiry_and_missed_target_cancel():
    tr = SimTrade(_setup(expiry=120_000), tick=0.1, time_stop_ms=10**13)
    assert step(tr, _bar(2, 101, 101.2, 100.5, 101), SimCosts()) == "cancelled"
    tr2 = SimTrade(_setup(), tick=0.1, time_stop_ms=10**13)
    assert step(tr2, _bar(1, 101, 102.2, 100.5, 102), SimCosts()) == "cancelled"
    assert "target" in tr2.exit_reason


def test_short_mirror_and_time_stop():
    c = SimCosts()
    tr = SimTrade(_setup("short", entry=100, sl=101, tp=98), tick=0.1, time_stop_ms=3 * 60000)
    assert step(tr, _bar(1, 99.8, 100.1, 99.7, 99.9), c) == "filled"
    assert step(tr, _bar(2, 99.9, 100.0, 99.5, 99.6), c) == "closed"         # bar closes at the time stop
    assert tr.exit_reason == "time stop" and tr.exit_price == pytest.approx(99.6 * (1 + c.exit_slip))
    assert tr.r_net(c) > 0


def test_summarize_expectancy():
    c = SimCosts()
    wins = []
    for k in range(3):
        tr = SimTrade(_setup(), tick=0.1, time_stop_ms=10**13)
        step(tr, _bar(1, 100.2, 100.3, 99.9, 100.1), c)
        step(tr, _bar(2, 101, 102.1, 100.5, 102) if k < 2 else _bar(2, 100, 100, 98.9, 99), c)
        wins.append(tr)
    s = summarize(wins, c)
    assert s["trades"] == 3 and s["win_rate"] == pytest.approx(2 / 3)
    assert s["exits"]["take profit"] == 2 and s["exits"]["stop loss"] == 1
    assert s["expectancy_r"] == pytest.approx(sum(t.r_net(c) for t in wins) / 3)
