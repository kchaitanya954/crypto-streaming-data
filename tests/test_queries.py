"""
Tests for database/queries.py — _ist_10am_window and get_daily_pnl_by_trigger.
Uses a real in-memory SQLite DB via the `db` fixture from conftest.py.
"""
import time
import datetime
import pytest
from database.queries import _ist_10am_window, get_daily_pnl_by_trigger


# ── _ist_10am_window ──────────────────────────────────────────────────────────

IST_OFFSET = 19800  # UTC+5:30

def _utc_from_ist(year, month, day, hour=0, minute=0):
    """Return UTC Unix timestamp from a naive IST datetime."""
    ist_dt = datetime.datetime(year, month, day, hour, minute, 0)
    utc_dt = ist_dt - datetime.timedelta(seconds=IST_OFFSET)
    return int((utc_dt - datetime.datetime(1970, 1, 1)).total_seconds())


def test_window_before_10am_ist():
    """Before 10am IST → window ends at yesterday's 10am IST (most recently completed)."""
    # Simulate 08:00 IST on Apr 23 = 02:30 UTC (today's 10am hasn't happened yet)
    utc_now = datetime.datetime(2026, 4, 23, 2, 30, 0)
    from_ts, to_ts, label = _ist_10am_window(utc_now)

    # to_ts should be 2026-04-22 10:00 IST = Apr 22 04:30 UTC
    expected_to = _utc_from_ist(2026, 4, 22, 10, 0)
    assert to_ts == expected_to

    # from_ts should be 24h earlier
    assert to_ts - from_ts == 86400


def test_window_after_10am_ist():
    """After 10am IST → window still ends at today 10am IST (completed window)."""
    # Simulate 15:00 IST = 09:30 UTC
    utc_now = datetime.datetime(2026, 4, 23, 9, 30, 0)
    from_ts, to_ts, label = _ist_10am_window(utc_now)

    expected_to = _utc_from_ist(2026, 4, 23, 10, 0)
    assert to_ts == expected_to
    assert to_ts - from_ts == 86400


def test_window_label_contains_ist():
    _, _, label = _ist_10am_window()
    assert "IST" in label
    assert "→" in label


def test_window_label_shows_10_00():
    _, _, label = _ist_10am_window(datetime.datetime(2026, 4, 23, 5, 0))
    assert "10:00" in label


def test_window_duration_exactly_24h():
    for hour in (0, 5, 10, 15, 23):
        utc_now = datetime.datetime(2026, 4, 24, hour, 0)
        f, t, _ = _ist_10am_window(utc_now)
        assert t - f == 86400, f"UTC hour={hour}"


# ── get_daily_pnl_by_trigger ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_window_returns_no_rows(db):
    rows = await get_daily_pnl_by_trigger(db, user_id=1)
    assert rows == []


@pytest.mark.asyncio
async def test_rows_include_grand_total(db):
    """When trades exist, last row is the grand total (trigger_id=None)."""
    # Insert minimal data: user, trigger, two orders inside the window
    await db.execute(
        "INSERT INTO users (id, username, email, password_hash, totp_secret, created_at) "
        "VALUES (1, 'u', 'u@e.com', 'x', 'x', 0)"
    )
    await db.execute(
        "INSERT INTO triggers (id, user_id, name, symbol, interval, min_confidence, "
        "trade_amount_usdt, active, market_type, leverage, created_at) "
        "VALUES (1, 1, 'T1', 'ETHUSDT', '5m', 'MEDIUM', 100, 1, 'spot', 1, 0)"
    )
    from_ts, to_ts, label = _ist_10am_window()
    mid_ts = (from_ts + to_ts) // 2  # timestamp safely inside the window
    # BUY inside window
    await db.execute(
        "INSERT INTO orders (user_id, trigger_id, symbol, side, order_type, quantity, "
        "price, status, created_at, updated_at) VALUES (1, 1, 'ETHUSDT', 'buy', 'market_order', "
        "0.05, 2000.0, 'filled', ?, ?)",
        (mid_ts, mid_ts),
    )
    # SELL inside window
    await db.execute(
        "INSERT INTO orders (id, user_id, trigger_id, symbol, side, order_type, quantity, "
        "price, status, created_at, updated_at) VALUES (2, 1, 1, 'ETHUSDT', 'sell', 'market_order', "
        "0.04, 2050.0, 'filled', ?, ?)",
        (mid_ts, mid_ts),
    )
    # trade_history entry for the SELL
    await db.execute(
        "INSERT INTO trade_history (order_id, symbol, side, "
        "filled_qty, filled_price, pnl, created_at) "
        "VALUES (2, 'ETHUSDT', 'sell', 0.04, 2050.0, 2.5, ?)",
        (mid_ts,),
    )
    await db.commit()
    rows = await get_daily_pnl_by_trigger(db, user_id=1, from_ts=from_ts, to_ts=to_ts)

    # At least one data row + total row
    assert len(rows) >= 2
    total = rows[-1]
    assert total["trigger_id"] is None
    assert total["symbol"] == "ALL"


@pytest.mark.asyncio
async def test_orders_outside_window_excluded(db):
    await db.execute(
        "INSERT INTO users (id, username, email, password_hash, totp_secret, created_at) "
        "VALUES (2, 'u2', 'u2@e.com', 'x', 'x', 0)"
    )
    await db.execute(
        "INSERT INTO triggers (id, user_id, name, symbol, interval, min_confidence, "
        "trade_amount_usdt, active, market_type, leverage, created_at) "
        "VALUES (2, 2, 'T2', 'BTCUSDT', '5m', 'HIGH', 200, 1, 'spot', 1, 0)"
    )
    old_ts = 1000  # Unix epoch 1970 — definitely outside any real window
    await db.execute(
        "INSERT INTO orders (user_id, trigger_id, symbol, side, order_type, quantity, "
        "price, status, created_at, updated_at) VALUES (2, 2, 'BTCUSDT', 'buy', 'market_order', "
        "0.001, 50000.0, 'filled', ?, ?)",
        (old_ts, old_ts),
    )
    await db.commit()

    from_ts, to_ts, label = _ist_10am_window()
    rows = await get_daily_pnl_by_trigger(db, user_id=2, from_ts=from_ts, to_ts=to_ts)
    assert rows == []


@pytest.mark.asyncio
async def test_true_pnl_usdt_computed(db):
    """true_pnl_usdt ≈ pnl_pct / 100 * sell_gross."""
    await db.execute(
        "INSERT INTO users (id, username, email, password_hash, totp_secret, created_at) "
        "VALUES (3, 'u3', 'u3@e.com', 'x', 'x', 0)"
    )
    await db.execute(
        "INSERT INTO triggers (id, user_id, name, symbol, interval, min_confidence, "
        "trade_amount_usdt, active, market_type, leverage, created_at) "
        "VALUES (3, 3, 'T3', 'ETHUSDT', '5m', 'HIGH', 100, 1, 'spot', 1, 0)"
    )
    from_ts, to_ts, label = _ist_10am_window()
    mid_ts = (from_ts + to_ts) // 2
    await db.execute(
        "INSERT INTO orders (id, user_id, trigger_id, symbol, side, order_type, "
        "quantity, price, status, created_at, updated_at) "
        "VALUES (10, 3, 3, 'ETHUSDT', 'sell', 'market_order', 0.05, 2000.0, 'filled', ?, ?)",
        (mid_ts, mid_ts),
    )
    # pnl = 2.0%, sell_gross = 0.05 * 2000 = 100 → true_pnl ≈ 2.0
    await db.execute(
        "INSERT INTO trade_history (order_id, symbol, side, "
        "filled_qty, filled_price, pnl, created_at) "
        "VALUES (10, 'ETHUSDT', 'sell', 0.05, 2000.0, 2.0, ?)",
        (mid_ts,),
    )
    await db.commit()

    rows = await get_daily_pnl_by_trigger(db, user_id=3, from_ts=from_ts, to_ts=to_ts)
    data = [r for r in rows if r["trigger_id"] is not None]
    assert len(data) == 1
    assert data[0]["true_pnl_usdt"] == pytest.approx(2.0 / 100.0 * 100.0, rel=0.01)
