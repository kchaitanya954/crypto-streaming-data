"""
Tests for exchange/ict_runner.py — position sizing, tick rounding and the paper-mode
trade lifecycle (setup → fill → take-profit) recorded in strategy_trades.
"""
import pytest

from database import queries
from exchange.ict_runner import ICTRunner, Spec, round_levels, size_position
from signals.ict_smt import Bar, Setup

SPEC = Spec(qty_step=0.001, min_qty=0.001, min_notional=60.0, tick=0.1)


def test_size_risk_based():
    qty, risk, why = size_position(1000, 5, 80000, 79600, SPEC, risk_pct=1.0)
    assert why == "" and qty == pytest.approx(0.025) and risk == pytest.approx(10.0)


def test_size_bumps_to_exchange_minimum_within_risk_cap():
    # $17 budget: 1% risk can't buy 0.001 BTC with a $400 stop, but 0.001 risks $0.40 (2.4%) ≤ 3%
    qty, risk, why = size_position(17, 5, 80000, 79600, SPEC, risk_pct=1.0, max_risk_pct=3.0)
    assert qty == pytest.approx(0.001) and risk == pytest.approx(0.40)


def test_size_skips_when_minimum_is_too_risky():
    qty, _, why = size_position(17, 5, 80000, 78000, SPEC, risk_pct=1.0, max_risk_pct=3.0)
    assert qty == 0 and "minimum size" in why


def test_size_never_exceeds_margin_budget():
    qty, _, _ = size_position(20, 2, 80000, 79990, SPEC, risk_pct=50.0, max_risk_pct=60.0)
    assert qty * 80000 / 2 <= 20


def _setup(direction="long", entry=100000.04, sl=99700.06, tp=100600.07):
    return Setup(direction=direction, entry=entry, sl=sl, tp=tp, rr=2.0, risk_pct=0.3,
                 created_ms=60_000, expiry_ms=60_000 + 30 * 60_000, tp_source="fallback",
                 smt_level="london_low", smt_swept_by="primary", bias=1, fvg_low=99900, fvg_high=100100)


def test_round_levels_conservative():
    s = round_levels(_setup(), 0.1)
    assert s.entry == pytest.approx(100000.0)
    assert s.sl == pytest.approx(99700.0)              # long stop floors (further away)
    assert s.tp == pytest.approx(100600.0)             # long target floors (closer)
    sh = round_levels(_setup("short", entry=100000.04, sl=100300.01, tp=99400.09), 0.1)
    assert sh.sl == pytest.approx(100300.1) and sh.tp == pytest.approx(99400.1)


@pytest.mark.asyncio
async def test_paper_trade_lifecycle(db):
    tid = await queries.create_trigger(db, "BTCUSDT", "5m", market_type="futures", leverage=5,
                                       trade_amount_usdt=1000, strategy="ict_smt", strategy_mode="paper")
    runner = ICTRunner(db, await queries.get_trigger(db, tid))
    runner.spec = SPEC
    await runner._open(_setup(), None, None)
    row = await queries.get_active_strategy_trade(db, tid)
    assert row["status"] == "pending" and row["mode"] == "paper" and row["cdx_order_id"] is None
    assert row["quantity"] == pytest.approx(0.033)    # 1% of $1000 / $300 stop, floored to 0.001

    await runner._manage_paper(Bar(120_000, 100100, 100150, 99990, 100050))   # trades through 100,000
    row = await queries.get_active_strategy_trade(db, tid)
    assert row["status"] == "open" and row["filled_at"] is not None

    await runner._manage_paper(Bar(180_000, 100300, 100650, 100250, 100620))  # target
    assert await queries.get_active_strategy_trade(db, tid) is None
    cur = await db.execute("SELECT * FROM strategy_trades WHERE trigger_id = ?", (tid,))
    done = dict(await cur.fetchone())
    assert done["status"] == "closed" and done["exit_reason"] == "take profit"
    # 2R target minus maker entry + taker exit + 1bp slippage ≈ 0.31R on a 0.3% stop
    exit_px = 100600 * (1 - 0.0001)
    net_per_unit = (exit_px - 100000) - (100000 * 0.000236 + exit_px * 0.00059)
    assert done["r_multiple"] == pytest.approx(net_per_unit / 300, abs=1e-3)
    assert done["pnl_usdt"] == pytest.approx(net_per_unit * 0.033, abs=1e-3)

    rows = await queries.get_strategy_trades_for_window(db, None, 0, 10**10)
    assert rows == []                                   # user-scoped: this trigger has no user


@pytest.mark.asyncio
async def test_paper_short_skipped_when_shorts_disabled(db):
    tid = await queries.create_trigger(db, "BTCUSDT", "5m", market_type="futures", leverage=5,
                                       trade_amount_usdt=1000, strategy="ict_smt", allow_shorts=False)
    runner = ICTRunner(db, await queries.get_trigger(db, tid))
    await runner._open(_setup("short", entry=100000, sl=100300, tp=99400), None, None)
    assert await queries.get_active_strategy_trade(db, tid) is None


def test_ict_triggers_ignore_indicator_signals():
    trig = {"symbol": "BTCUSDT", "interval": "5m", "min_confidence": "MEDIUM", "strategy": "ict_smt"}
    assert not queries.trigger_matches(trig, "BTCUSDT", "5m", "HIGH")
    trig["strategy"] = "macd"
    assert queries.trigger_matches(trig, "BTCUSDT", "5m", "HIGH")
