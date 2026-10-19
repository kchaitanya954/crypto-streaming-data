"""
End-to-end tests for the entry guards in execute_futures_trigger_trade, with a fake
CoinDCX client: exit signals close only, shorts can be disabled, and a pair that is
already held (by another trigger or untracked on the exchange) is never stacked on.
"""
import time

import pytest

import auth.encryption
import exchange.auto_trade
import exchange.coindcx_client
from database import queries
from exchange.futures_trade import execute_futures_trigger_trade
from signals.adaptive_strategy import engine as adaptive_engine
from signals.detector import Signal


class _FakeClient:
    live = None
    orders: list = []
    exits: list = []

    def __init__(self, api_key, api_secret, session):
        pass

    _futures_pair = staticmethod(exchange.coindcx_client.CoinDCXClient._futures_pair)

    async def get_futures_positions(self, pair=None):
        return []

    async def get_active_futures_position(self, symbol):
        return _FakeClient.live

    async def get_futures_funding_rate(self, pair):
        return None

    async def create_futures_order(self, **kw):
        _FakeClient.orders.append(kw)
        _FakeClient.live = {"id": "p-new", "active_pos": kw["quantity"] * (1 if kw["side"] == "buy" else -1)}
        return {"id": "ord-1"}

    async def exit_futures_position(self, position_id):
        _FakeClient.exits.append(position_id)
        _FakeClient.live = None
        return {"data": {"group_id": "g1"}}

    async def close_futures_position(self, side, pair, quantity, leverage):
        _FakeClient.orders.append({"side": side, "quantity": quantity, "close": True})
        return {"id": "close-1"}


@pytest.fixture
async def env(db, monkeypatch):
    _FakeClient.live, _FakeClient.orders, _FakeClient.exits = None, [], []
    monkeypatch.setattr(exchange.coindcx_client, "CoinDCXClient", _FakeClient)
    monkeypatch.setattr(auth.encryption, "safe_decrypt", lambda v: v)

    async def _no_tg(_settings):
        return None, None
    monkeypatch.setattr(exchange.auto_trade, "_get_user_telegram", _no_tg)
    monkeypatch.setattr(adaptive_engine, "buy_pct_for", lambda conf, adx=None: 100.0)
    uid = await queries.create_user(db, "u", "u@x.io", "h", "t")
    await queries.upsert_user_settings(db, uid, coindcx_api_key_enc="k", coindcx_api_secret_enc="s")
    return db, uid


async def _trigger(db, uid, **kw):
    tid = await queries.create_trigger(db, "BTCUSDT", "1h", market_type="futures", leverage=5,
                                       trade_amount_usdt=100, user_id=uid, **kw)
    return await queries.get_trigger(db, tid)


def _sig(direction, is_exit=False):
    return Signal(direction=direction, confidence="HIGH", entry_price=80000.0, open_time=0,
                  macd_val=0, signal_val=0, histogram=0, adx_val=30, trend_note="t",
                  reasons=["a", "b", "c"], is_exit=is_exit)


@pytest.mark.asyncio
async def test_entry_signal_opens(env):
    db, uid = env
    trig = await _trigger(db, uid)
    await execute_futures_trigger_trade(db, trig, _sig("BUY"), "BTCUSDT", "1h")
    assert len(_FakeClient.orders) == 1 and _FakeClient.orders[0]["side"] == "buy"
    assert await queries.get_open_futures_position(db, trig["id"])


@pytest.mark.asyncio
async def test_exit_signal_never_opens(env):
    db, uid = env
    trig = await _trigger(db, uid)
    await execute_futures_trigger_trade(db, trig, _sig("SELL", is_exit=True), "BTCUSDT", "1h")
    assert _FakeClient.orders == []


@pytest.mark.asyncio
async def test_exit_signal_closes_but_does_not_reverse(env):
    db, uid = env
    trig = await _trigger(db, uid)
    pid = await queries.insert_futures_position(db, trig["id"], uid, "BTCUSDT", "long", 0.006, 80000, 5,
                                                96, 64000, 79000, 82000)
    await db.execute("UPDATE futures_positions SET created_at = ? WHERE id = ?", (int(time.time()) - 3600, pid))
    await db.commit()
    _FakeClient.live = {"id": "p1", "active_pos": 0.006}
    await execute_futures_trigger_trade(db, trig, _sig("SELL", is_exit=True), "BTCUSDT", "1h")
    assert _FakeClient.exits == ["p1"]               # closed by position id
    assert _FakeClient.orders == []                  # …and no short opened
    assert await queries.get_open_futures_position(db, trig["id"]) is None


@pytest.mark.asyncio
async def test_shorts_disabled(env):
    db, uid = env
    trig = await _trigger(db, uid, allow_shorts=False)
    await execute_futures_trigger_trade(db, trig, _sig("SELL"), "BTCUSDT", "1h")
    assert _FakeClient.orders == []


@pytest.mark.asyncio
async def test_untracked_exchange_position_blocks_entry(env):
    db, uid = env
    trig = await _trigger(db, uid)
    _FakeClient.live = {"id": "manual", "active_pos": -0.002}    # e.g. a manual short in the app
    await execute_futures_trigger_trade(db, trig, _sig("BUY"), "BTCUSDT", "1h")
    assert _FakeClient.orders == []


@pytest.mark.asyncio
async def test_pair_held_by_another_trigger_blocks_entry(env):
    db, uid = env
    a = await _trigger(db, uid)
    b = await _trigger(db, uid)
    await queries.insert_futures_position(db, a["id"], uid, "BTCUSDT", "long", 0.006, 80000, 5,
                                          96, 64000, 79000, 82000)
    await execute_futures_trigger_trade(db, b, _sig("SELL"), "BTCUSDT", "1h")
    assert _FakeClient.orders == []                  # no opposite order netting the pair to zero
