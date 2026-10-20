"""
Tests for signals/trend.py and exchange/trend_runner.py (daily Donchian, spot).
"""
import json

import pytest

import exchange.trend_runner as tr_mod
from database import queries
from exchange.trend_runner import TrendRunner
from signals.trend import SYSTEMS, decide, levels, replay_state
from streaming.stream import Kline

DAY = 86_400_000


def _bars(closes, start=1_700_000_000_000 // DAY * DAY):
    return [Kline("BTCUSDT", "1d", start + i * DAY, start + (i + 1) * DAY - 1, c, c * 1.001, c * 0.999, c, 1.0, True, 0)
            for i, c in enumerate(closes)]


def test_levels_use_prior_bars_only():
    highs, lows = [1, 2, 3, 10], [0, 1, 2, 9]
    assert levels(highs, lows, 3, 2) == (3, 1)          # today's 10/9 excluded
    assert levels(highs[:3], lows[:3], 3, 2) == (None, None)


def test_decide_breakout_and_breakdown():
    flat = [100.0] * 30
    up = flat + [101.0]
    assert decide([c * 1.001 for c in up], [c * 0.999 for c in up], up, 20, 10, holding=False) == "buy"
    assert decide([c * 1.001 for c in up], [c * 0.999 for c in up], up, 20, 10, holding=True) is None
    down = flat + [98.0]
    assert decide([c * 1.001 for c in down], [c * 0.999 for c in down], down, 20, 10, holding=True) == "sell"
    assert decide([c * 1.001 for c in down], [c * 0.999 for c in down], down, 20, 10, holding=False) is None


def test_replay_state():
    closes = [100.0] * 30 + [110.0] + [111.0] * 5
    assert replay_state([c * 1.001 for c in closes], [c * 0.999 for c in closes], closes, 20, 10)
    closes += [90.0]
    assert not replay_state([c * 1.001 for c in closes], [c * 0.999 for c in closes], closes, 20, 10)


async def _trigger(db, mode="paper", budget=45.0, user_id=None):
    tid = await queries.create_trigger(db, "BTCUSDT", "1d", market_type="spot", trade_amount_usdt=budget,
                                       strategy="trend_donchian", strategy_mode=mode, user_id=user_id)
    return await queries.get_trigger(db, tid)


async def _open(db, tid):
    cur = await db.execute("SELECT * FROM strategy_trades WHERE trigger_id = ? AND status = 'open'", (tid,))
    return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_paper_buy_once_then_sell(db):
    trig = await _trigger(db)
    r = TrendRunner(db, trig)
    base = [100.0] * 120
    # breakout above every system's prior high → three one-third buys
    bars = _bars(base + [105.0])
    assert sorted(await r.evaluate(bars)) == sorted(f"BUY {n}/{m}" for n, m in SYSTEMS)
    assert await r.evaluate(bars) == []                 # same day again: no double buy
    rows = await _open(db, trig["id"])
    assert len(rows) == 3 and all(x["quantity"] == pytest.approx(15 / (105 * 1.002), abs=1e-5) for x in rows)
    # a close below the 10-day low exits only the 20/10 system
    bars2 = _bars(base + [105.0] * 12 + [103.0])
    assert await r.evaluate(bars2) == ["SELL 20/10"]
    left = {json.loads(x["setup_json"])["system"] for x in await _open(db, trig["id"])}
    assert left == {"50/25", "100/50"}
    cur = await db.execute("SELECT pnl_usdt, exit_reason FROM strategy_trades WHERE status = 'closed'")
    pnl, why = await cur.fetchone()
    assert pnl < 0 and "10-day low" in why               # 105 → 103 minus 0.4% fees


@pytest.mark.asyncio
async def test_below_exchange_minimum_is_skipped(db):
    r = TrendRunner(db, await _trigger(db, budget=9.0))   # $3 per system < $5 minimum
    assert await r.evaluate(_bars([100.0] * 120 + [105.0])) == []


class _FakeSpot:
    def __init__(self, usdt=100.0, btc=0.0):
        self.usdt, self.btc, self.orders = usdt, btc, []

    async def get_balances(self):
        return [{"currency": "USDT", "balance": self.usdt}, {"currency": "BTC", "balance": self.btc}]

    async def create_order(self, side, market, order_type, quantity):
        self.orders.append((side, quantity))
        self.btc += quantity if side == "buy" else -quantity
        return {"orders": [{"id": f"o{len(self.orders)}"}]}

    async def get_spot_fills(self, **kw):
        oid = f"o{len(self.orders)}"
        side, q = self.orders[-1]
        return [{"order_id": oid, "quantity": str(q), "price": "105.5", "fee_amount": "0.0317"}]


@pytest.mark.asyncio
async def test_live_uses_real_fills_and_sells_only_own_coins(db, monkeypatch):
    fake = _FakeSpot(usdt=100.0)
    trig = await _trigger(db, mode="live")
    r = TrendRunner(db, trig)

    async def call(fn, *a, **kw):
        return await getattr(fake, fn)(*a, **kw)
    monkeypatch.setattr(r, "_client_call", call)
    monkeypatch.setattr(tr_mod.asyncio, "sleep", lambda s: _noop())

    base = [100.0] * 120
    await r.evaluate(_bars(base + [105.0]))
    assert [o[0] for o in fake.orders] == ["buy", "buy", "buy"]
    rows = await _open(db, trig["id"])
    assert all(x["entry_price"] == pytest.approx(105.5) and x["cdx_order_id"] for x in rows)

    fake.btc -= rows[0]["quantity"] * 0.5               # user sold some coins by hand
    await r.evaluate(_bars(base + [105.0] * 12 + [103.0]))
    sell_qty = fake.orders[-1][1]
    assert fake.orders[-1][0] == "sell" and sell_qty <= rows[0]["quantity"] + 1e-12


async def _noop():
    return None


@pytest.mark.asyncio
async def test_live_buy_skipped_without_usdt(db, monkeypatch):
    fake = _FakeSpot(usdt=2.0)
    r = TrendRunner(db, await _trigger(db, mode="live"))

    async def call(fn, *a, **kw):
        return await getattr(fake, fn)(*a, **kw)
    monkeypatch.setattr(r, "_client_call", call)
    assert await r.evaluate(_bars([100.0] * 120 + [105.0])) == []
    assert fake.orders == []


def test_trend_triggers_ignore_indicator_signals():
    trig = {"symbol": "BTCUSDT", "interval": "1d", "min_confidence": "MEDIUM", "strategy": "trend_donchian"}
    assert not queries.trigger_matches(trig, "BTCUSDT", "1d", "HIGH")
