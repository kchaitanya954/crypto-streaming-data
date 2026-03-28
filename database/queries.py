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


async def get_recent_signals(
    db: aiosqlite.Connection,
    symbol: str,
    interval: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Return recent signals for a symbol, newest first."""
    if interval:
        cursor = await db.execute(
            """
            SELECT * FROM signals
            WHERE symbol = ? AND interval = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (symbol.upper(), interval, limit),
        )
    else:
        cursor = await db.execute(
            """
            SELECT * FROM signals
            WHERE symbol = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (symbol.upper(), limit),
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
) -> int:
    """Insert a new trigger. Returns the new row id."""
    cursor = await db.execute(
        "INSERT INTO triggers (symbol, interval, min_confidence, active, created_at) VALUES (?, ?, ?, 1, ?)",
        (symbol.upper(), interval, min_confidence.upper(), int(time.time())),
    )
    await db.commit()
    return cursor.lastrowid


async def update_trigger(
    db: aiosqlite.Connection,
    trigger_id: int,
    symbol: Optional[str] = None,
    interval: Optional[str] = None,
    min_confidence: Optional[str] = None,
    active: Optional[bool] = None,
) -> None:
    """Update any subset of trigger fields."""
    fields, params = [], []
    if symbol         is not None: fields.append("symbol = ?");         params.append(symbol.upper())
    if interval       is not None: fields.append("interval = ?");       params.append(interval)
    if min_confidence is not None: fields.append("min_confidence = ?"); params.append(min_confidence.upper())
    if active         is not None: fields.append("active = ?");         params.append(1 if active else 0)
    if not fields:
        return
    params.append(trigger_id)
    await db.execute(f"UPDATE triggers SET {', '.join(fields)} WHERE id = ?", params)
    await db.commit()


async def delete_trigger(db: aiosqlite.Connection, trigger_id: int) -> None:
    """Hard-delete a trigger row."""
    await db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    await db.commit()


def trigger_matches(trigger: dict, symbol: str, interval: str, confidence: str) -> bool:
    """Return True if the signal meets the trigger's criteria."""
    return (
        trigger["symbol"].upper() == symbol.upper()
        and trigger["interval"] == interval
        and _CONF_ORDER.get(confidence.upper(), 0) >= _CONF_ORDER.get(trigger["min_confidence"].upper(), 0)
    )
