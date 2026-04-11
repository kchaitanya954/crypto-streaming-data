"""
Typed async query functions for all database operations.

All functions accept an aiosqlite.Connection as their first argument.
"""

import json
import time
from typing import Optional

import aiosqlite

from streaming.stream import Kline
from signals.detector import Signal


async def insert_candle(
    db: aiosqlite.Connection,
    symbol: str,
    interval: str,
    kline: Kline,
) -> None:
    """Insert or ignore a closed candle (skips duplicates via UNIQUE constraint)."""
    await db.execute(
        """
        INSERT OR IGNORE INTO candles
            (symbol, interval, open_time, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (symbol.upper(), interval, kline.open_time, kline.open,
         kline.high, kline.low, kline.close, kline.volume),
    )
    await db.commit()


async def insert_signal(
    db: aiosqlite.Connection,
    symbol: str,
    interval: str,
    signal: Signal,
) -> int:
    """Persist a detected signal. Returns the new row id."""
    cursor = await db.execute(
        """
        INSERT INTO signals
            (symbol, interval, open_time, direction, confidence, entry_price,
             macd_val, signal_val, histogram, adx_val, trend_note, reasons, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol.upper(), interval, signal.open_time,
            signal.direction, signal.confidence, signal.entry_price,
            signal.macd_val, signal.signal_val, signal.histogram,
            signal.adx_val, signal.trend_note,
            json.dumps(signal.reasons),
            int(time.time()),
        ),
    )
    await db.commit()
    return cursor.lastrowid


async def insert_order(
    db: aiosqlite.Connection,
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: Optional[float] = None,
    signal_id: Optional[int] = None,
    telegram_msg_id: Optional[int] = None,
) -> int:
    """Insert a new order record. Returns the new row id."""
    now = int(time.time())
    cursor = await db.execute(
        """
        INSERT INTO orders
            (signal_id, symbol, side, order_type, quantity, price,
             status, telegram_msg_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (signal_id, symbol.upper(), side, order_type, quantity, price,
         telegram_msg_id, now, now),
    )
    await db.commit()
    return cursor.lastrowid


async def update_order_status(
    db: aiosqlite.Connection,
    order_id: int,
    status: str,
    coindcx_order_id: Optional[str] = None,
) -> None:
    """Update order status (and optionally the CoinDCX order id)."""
    await db.execute(
        """
        UPDATE orders
        SET status = ?, coindcx_order_id = COALESCE(?, coindcx_order_id),
            updated_at = ?
        WHERE id = ?
        """,
        (status, coindcx_order_id, int(time.time()), order_id),
    )
    await db.commit()


async def insert_trade(
    db: aiosqlite.Connection,
    order_id: int,
    symbol: str,
    side: str,
    filled_qty: float,
    filled_price: float,
    fee: Optional[float] = None,
    fee_currency: Optional[str] = None,
    pnl: Optional[float] = None,
) -> int:
    """Record a completed trade fill. Returns the new row id."""
    cursor = await db.execute(
        """
        INSERT INTO trade_history
            (order_id, symbol, side, filled_qty, filled_price,
             fee, fee_currency, pnl, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, symbol.upper(), side, filled_qty, filled_price,
         fee, fee_currency, pnl, int(time.time())),
    )
    await db.commit()
    return cursor.lastrowid


async def delete_signal(db: aiosqlite.Connection, signal_id: int) -> None:
    """Hard-delete a single signal row."""
    await db.execute("DELETE FROM signals WHERE id = ?", (signal_id,))
    await db.commit()


async def delete_signals(
    db: aiosqlite.Connection,
    symbol:     Optional[str] = None,
    interval:   Optional[str] = None,
    direction:  Optional[str] = None,
    confidence: Optional[str] = None,
    before_ts:  Optional[int] = None,   # Unix seconds — delete signals created before this
) -> int:
    """Bulk-delete signals matching any combination of filters. Returns row count deleted."""
    conditions, params = [], []
    if symbol:     conditions.append("symbol = ?");    params.append(symbol.upper())
    if interval:   conditions.append("interval = ?");  params.append(interval)
    if direction:  conditions.append("direction = ?"); params.append(direction.upper())
    if confidence: conditions.append("confidence = ?");params.append(confidence.upper())
    if before_ts:  conditions.append("created_at < ?");params.append(before_ts)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await db.execute(f"DELETE FROM signals {where}", params)
    await db.commit()
    return cursor.rowcount


async def get_recent_signals(
    db: aiosqlite.Connection,
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Return recent signals, newest first. All filters are optional."""
    conditions, params = [], []
    if symbol:   conditions.append("symbol = ?");   params.append(symbol.upper())
    if interval: conditions.append("interval = ?"); params.append(interval)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await db.execute(
        f"SELECT * FROM signals {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["reasons"] = json.loads(d["reasons"]) if d["reasons"] else []
        result.append(d)
    return result


async def get_signals_for_analytics(
    db: aiosqlite.Connection,
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
    confidence: Optional[str] = None,
    since: Optional[int] = None,
) -> list[dict]:
    """Return signals for analytics (oldest-first), filtered by params."""
    conditions, params = [], []
    if symbol:
        conditions.append("symbol = ?"); params.append(symbol.upper())
    if interval:
        conditions.append("interval = ?"); params.append(interval)
    if confidence:
        conditions.append("confidence = ?"); params.append(confidence.upper())
    if since:
        conditions.append("created_at >= ?"); params.append(since)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await db.execute(
        f"SELECT * FROM signals {where} ORDER BY open_time ASC", params
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["reasons"] = json.loads(d["reasons"]) if d["reasons"] else []
        result.append(d)
    return result


async def get_open_orders(db: aiosqlite.Connection) -> list[dict]:
    """Return all orders with status 'open' or 'pending'."""
    cursor = await db.execute(
        "SELECT * FROM orders WHERE status IN ('open', 'pending') ORDER BY created_at DESC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def get_trade_history(
    db: aiosqlite.Connection,
    limit: int = 100,
) -> list[dict]:
    """Return recent trade fills, newest first."""
    cursor = await db.execute(
        "SELECT * FROM trade_history ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


_CONF_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


async def get_trigger(db: aiosqlite.Connection, trigger_id: int) -> Optional[dict]:
    """Fetch a single trigger by id."""
    cursor = await db.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_triggers(db: aiosqlite.Connection, active_only: bool = True) -> list[dict]:
    """Return all triggers (active only by default)."""
    if active_only:
        cursor = await db.execute(
            "SELECT * FROM triggers WHERE active = 1 ORDER BY created_at DESC"
        )
    else:
        cursor = await db.execute("SELECT * FROM triggers ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def create_trigger(
    db: aiosqlite.Connection,
    symbol: str,
    interval: str,
    min_confidence: str = "MEDIUM",
    adx_threshold: Optional[float] = None,
    cooldown_bars: Optional[int] = None,
    name: Optional[str] = None,
) -> int:
    """Insert a new trigger. Returns the new row id."""
    cursor = await db.execute(
        """INSERT INTO triggers
               (symbol, interval, min_confidence, adx_threshold, cooldown_bars, name, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
        (symbol.upper(), interval, min_confidence.upper(),
         adx_threshold, cooldown_bars, name, int(time.time())),
    )
    await db.commit()
    return cursor.lastrowid


_SENTINEL = object()


async def update_trigger(
    db: aiosqlite.Connection,
    trigger_id: int,
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
    min_confidence: Optional[str] = None,
    active: Optional[bool] = None,
    adx_threshold = _SENTINEL,
    cooldown_bars  = _SENTINEL,
    name: Optional[str] = None,
) -> None:
    """Update any subset of trigger fields."""
    fields, params = [], []
    if symbol         is not None:      fields.append("symbol = ?");         params.append(symbol.upper())
    if interval       is not None:      fields.append("interval = ?");       params.append(interval)
    if min_confidence is not None:      fields.append("min_confidence = ?"); params.append(min_confidence.upper())
    if active         is not None:      fields.append("active = ?");         params.append(1 if active else 0)
    if adx_threshold  is not _SENTINEL: fields.append("adx_threshold = ?");  params.append(adx_threshold)
    if cooldown_bars  is not _SENTINEL: fields.append("cooldown_bars = ?");  params.append(cooldown_bars)
    if name           is not None:      fields.append("name = ?");           params.append(name)
    if not fields:
        return
    params.append(trigger_id)
    await db.execute(f"UPDATE triggers SET {', '.join(fields)} WHERE id = ?", params)
    await db.commit()


async def delete_trigger(db: aiosqlite.Connection, trigger_id: int) -> None:
    """Hard-delete a trigger row."""
    await db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    await db.commit()


async def load_adaptive_state(db: aiosqlite.Connection) -> Optional[dict]:
    """Load persisted adaptive engine state. Returns None if not yet saved."""
    cursor = await db.execute("SELECT state_json FROM adaptive_state WHERE id = 1")
    row = await cursor.fetchone()
    if row:
        return json.loads(row[0])
    return None


async def save_adaptive_state(db: aiosqlite.Connection, state_dict: dict) -> None:
    """Upsert adaptive engine state to DB."""
    await db.execute(
        "INSERT OR REPLACE INTO adaptive_state (id, state_json, updated_at) VALUES (1, ?, ?)",
        (json.dumps(state_dict), int(time.time())),
    )
    await db.commit()


def trigger_matches(
    trigger: dict,
    symbol: str,
    interval: str,
    confidence: str,
    adx_val: Optional[float] = None,
) -> bool:
    """Return True if the signal meets the trigger's criteria."""
    if trigger["symbol"].upper() != symbol.upper():
        return False
    if trigger["interval"] != interval:
        return False
    if _CONF_ORDER.get(confidence.upper(), 0) < _CONF_ORDER.get(trigger["min_confidence"].upper(), 0):
        return False
    # Per-trigger ADX filter: if set, signal's ADX must meet or exceed it
    trig_adx = trigger.get("adx_threshold")
    if trig_adx and adx_val is not None and adx_val < trig_adx:
        return False
    return True
