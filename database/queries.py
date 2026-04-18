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
    user_id: Optional[int] = None,
    trigger_id: Optional[int] = None,
) -> int:
    """Insert a new order record. Returns the new row id."""
    now = int(time.time())
    cursor = await db.execute(
        """
        INSERT INTO orders
            (signal_id, symbol, side, order_type, quantity, price,
             status, telegram_msg_id, user_id, trigger_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (signal_id, symbol.upper(), side, order_type, quantity, price,
         telegram_msg_id, user_id, trigger_id, now, now),
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
    user_id: Optional[int] = None,
) -> list[dict]:
    """Return recent signals, newest first. Filtered by user's triggers when user_id given."""
    conditions, params = [], []
    if symbol:   conditions.append("symbol = ?");   params.append(symbol.upper())
    if interval: conditions.append("interval = ?"); params.append(interval)
    if user_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM triggers WHERE triggers.user_id = ?"
            " AND triggers.symbol = signals.symbol AND triggers.interval = signals.interval)"
        )
        params.append(user_id)
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
    user_id: Optional[int] = None,
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
    if user_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM triggers WHERE triggers.user_id = ?"
            " AND triggers.symbol = signals.symbol AND triggers.interval = signals.interval)"
        )
        params.append(user_id)
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


async def get_triggers(
    db: aiosqlite.Connection,
    active_only: bool = True,
    user_id: Optional[int] = None,
) -> list[dict]:
    """Return triggers, optionally filtered by active status and/or user."""
    conditions, params = [], []
    if active_only:
        conditions.append("active = 1")
    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    cursor = await db.execute(
        f"SELECT * FROM triggers {where} ORDER BY created_at DESC", params
    )
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
    trade_amount_usdt: Optional[float] = None,
    user_id: Optional[int] = None,
) -> int:
    """Insert a new trigger. Returns the new row id."""
    cursor = await db.execute(
        """INSERT INTO triggers
               (symbol, interval, min_confidence, adx_threshold, cooldown_bars,
                name, trade_amount_usdt, user_id, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (symbol.upper(), interval, min_confidence.upper(),
         adx_threshold, cooldown_bars, name,
         trade_amount_usdt, user_id, int(time.time())),
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
    trade_amount_usdt: Optional[float] = None,
) -> None:
    """Update any subset of trigger fields."""
    fields, params = [], []
    if symbol             is not None:      fields.append("symbol = ?");             params.append(symbol.upper())
    if interval           is not None:      fields.append("interval = ?");           params.append(interval)
    if min_confidence     is not None:      fields.append("min_confidence = ?");     params.append(min_confidence.upper())
    if active             is not None:      fields.append("active = ?");             params.append(1 if active else 0)
    if adx_threshold      is not _SENTINEL: fields.append("adx_threshold = ?");      params.append(adx_threshold)
    if cooldown_bars      is not _SENTINEL: fields.append("cooldown_bars = ?");      params.append(cooldown_bars)
    if name               is not None:      fields.append("name = ?");               params.append(name)
    if trade_amount_usdt  is not None:      fields.append("trade_amount_usdt = ?");  params.append(trade_amount_usdt)
    if not fields:
        return
    params.append(trigger_id)
    await db.execute(f"UPDATE triggers SET {', '.join(fields)} WHERE id = ?", params)
    await db.commit()


async def delete_trigger(db: aiosqlite.Connection, trigger_id: int) -> None:
    """Hard-delete a trigger row."""
    await db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
    await db.commit()


async def get_real_trades_for_user(
    db: aiosqlite.Connection,
    user_id: int,
    since: Optional[int] = None,
) -> list[dict]:
    """
    Return all filled orders for a user, ordered by time.
    Includes USDT amount (qty × price) for each side.
    """
    conditions = ["o.user_id = ?", "o.status = 'filled'", "o.price IS NOT NULL"]
    params: list = [user_id]
    if since:
        conditions.append("o.created_at >= ?")
        params.append(since)
    where = " AND ".join(conditions)
    cursor = await db.execute(
        f"""
        SELECT
            o.id, o.symbol, o.side, o.quantity, o.price,
            o.trigger_id, o.created_at,
            ROUND(o.quantity * o.price, 6) AS usdt_amount,
            th.pnl AS pnl_pct
        FROM orders o
        LEFT JOIN trade_history th ON th.order_id = o.id
        WHERE {where}
        ORDER BY o.created_at ASC
        """,
        params,
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_real_completed_pnls(db: aiosqlite.Connection) -> list[dict]:
    """
    Return all completed trade P&Ls from trade_history (real executed SELLs).
    Only includes rows where pnl IS NOT NULL (i.e. a BUY position existed before the SELL).
    Used by the adaptive monitor — learns only from actual CoinDCX executions.
    """
    cursor = await db.execute(
        """
        SELECT th.pnl AS pnl_pct, th.created_at
        FROM trade_history th
        WHERE th.pnl IS NOT NULL
        ORDER BY th.created_at ASC
        """
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_daily_pnl_by_trigger(
    db: aiosqlite.Connection,
    user_id: int,
    date_iso: Optional[str] = None,  # "YYYY-MM-DD" in IST; defaults to today IST
) -> list[dict]:
    """
    Return per-trigger P&L summary for a given IST calendar day.

    Each row:
        trigger_id, symbol, interval,
        buy_count,  buy_usdt  (gross),  buy_cost_eff  (incl. 0.1% fee),
        sell_count, sell_usdt (gross),  sell_net_eff  (after 0.1% fee),
        avg_pnl_pct (fee-adjusted, from trade_history),
        fee_usdt, net_pnl_usdt (fee-adjusted)
    Plus a final synthetic row with trigger_id=None for the grand total.
    """
    import datetime as _dt

    FEE = 0.001          # CoinDCX taker fee per side (0.1%)
    IST_OFFSET = 19800   # UTC+5:30 in seconds

    if date_iso is None:
        ist_now  = _dt.datetime.utcnow() + _dt.timedelta(seconds=IST_OFFSET)
        date_iso = ist_now.strftime("%Y-%m-%d")

    # Gross amounts from orders table; fee-adjusted pnl from trade_history
    cursor = await db.execute(
        """
        SELECT
            o.trigger_id,
            t.symbol,
            t.interval,
            SUM(CASE WHEN o.side = 'buy'  THEN 1   ELSE 0 END) AS buy_count,
            SUM(CASE WHEN o.side = 'buy'  THEN COALESCE(o.quantity * o.price, 0) ELSE 0 END) AS buy_usdt_gross,
            SUM(CASE WHEN o.side = 'sell' THEN 1   ELSE 0 END) AS sell_count,
            SUM(CASE WHEN o.side = 'sell' THEN COALESCE(th.filled_qty * th.filled_price, 0) ELSE 0 END) AS sell_usdt_gross,
            AVG(CASE WHEN o.side = 'sell' THEN th.pnl ELSE NULL END) AS avg_pnl_pct
        FROM orders o
        JOIN triggers t ON t.id = o.trigger_id
        LEFT JOIN trade_history th ON th.order_id = o.id
        WHERE o.user_id    = ?
          AND o.status     = 'filled'
          AND DATE((o.created_at / 1000) + ?, 'unixepoch') = ?
        GROUP BY o.trigger_id
        ORDER BY o.trigger_id
        """,
        (user_id, IST_OFFSET, date_iso),
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    for r in rows:
        buy_gross  = r.get("buy_usdt_gross")  or 0.0
        sell_gross = r.get("sell_usdt_gross") or 0.0
        # Fee-adjusted effective amounts
        buy_eff  = round(buy_gross  * (1 + FEE), 4)   # actual money paid
        sell_eff = round(sell_gross * (1 - FEE), 4)   # actual money received
        fee_paid = round(buy_gross * FEE + sell_gross * FEE, 4)
        r["buy_usdt"]     = round(buy_gross, 4)
        r["sell_usdt"]    = round(sell_gross, 4)
        r["buy_cost_eff"] = buy_eff
        r["sell_net_eff"] = sell_eff
        r["fee_usdt"]     = fee_paid
        r["net_pnl_usdt"] = round(sell_eff - buy_eff, 4)
        r["avg_pnl_pct"]  = round(r["avg_pnl_pct"], 4) if r["avg_pnl_pct"] is not None else None

    # Grand-total row
    if rows:
        total = {
            "trigger_id":   None,
            "symbol":       "ALL",
            "interval":     "—",
            "buy_count":    sum(r["buy_count"]  for r in rows),
            "buy_usdt":     round(sum(r["buy_usdt"]    for r in rows), 4),
            "sell_count":   sum(r["sell_count"] for r in rows),
            "sell_usdt":    round(sum(r["sell_usdt"]   for r in rows), 4),
            "buy_cost_eff": round(sum(r["buy_cost_eff"] for r in rows), 4),
            "sell_net_eff": round(sum(r["sell_net_eff"] for r in rows), 4),
            "fee_usdt":     round(sum(r["fee_usdt"]    for r in rows), 4),
            "avg_pnl_pct":  None,
            "net_pnl_usdt": round(sum(r["net_pnl_usdt"] for r in rows), 4),
        }
        rows.append(total)

    return rows


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


# ── User queries ────────────────────────────────────────────────────────────────

async def create_user(
    db: aiosqlite.Connection,
    username: str,
    email: str,
    password_hash: str,
    totp_secret: str,
) -> int:
    """Insert a new user. Returns the new row id."""
    cursor = await db.execute(
        """INSERT INTO users (username, email, password_hash, totp_secret, totp_enabled, created_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (username.strip(), email.strip().lower(), password_hash, totp_secret, int(time.time())),
    )
    await db.commit()
    return cursor.lastrowid


async def get_all_users(db: aiosqlite.Connection) -> list[dict]:
    """Return all registered users (id, username, email)."""
    cursor = await db.execute("SELECT id, username, email FROM users")
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_user_by_username(db: aiosqlite.Connection, username: str) -> Optional[dict]:
    cursor = await db.execute("SELECT * FROM users WHERE username = ?", (username.strip(),))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_user_by_id(db: aiosqlite.Connection, user_id: int) -> Optional[dict]:
    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def enable_totp(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user_id,))
    await db.commit()


async def get_user_by_email(db: aiosqlite.Connection, email: str) -> Optional[dict]:
    cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def update_password(db: aiosqlite.Connection, user_id: int, new_hash: str) -> None:
    await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id))
    await db.commit()


async def set_password_reset_token(
    db: aiosqlite.Connection, user_id: int, token: str, expiry: int
) -> None:
    await db.execute(
        "UPDATE users SET password_reset_token = ?, password_reset_expiry = ? WHERE id = ?",
        (token, expiry, user_id),
    )
    await db.commit()


async def get_user_by_reset_token(db: aiosqlite.Connection, token: str) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT * FROM users WHERE password_reset_token = ?", (token,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def clear_password_reset_token(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "UPDATE users SET password_reset_token = NULL, password_reset_expiry = NULL WHERE id = ?",
        (user_id,),
    )
    await db.commit()


# ── User settings queries ────────────────────────────────────────────────────────

async def get_user_settings(db: aiosqlite.Connection, user_id: int) -> Optional[dict]:
    cursor = await db.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_user_settings(
    db: aiosqlite.Connection,
    user_id: int,
    telegram_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    coindcx_api_key_enc: Optional[str] = None,
    coindcx_api_secret_enc: Optional[str] = None,
) -> None:
    """Insert or update user settings, preserving existing values for omitted fields."""
    existing = await get_user_settings(db, user_id)
    if existing:
        await db.execute(
            """UPDATE user_settings SET
               telegram_token         = COALESCE(?, telegram_token),
               telegram_chat_id       = COALESCE(?, telegram_chat_id),
               coindcx_api_key_enc    = COALESCE(?, coindcx_api_key_enc),
               coindcx_api_secret_enc = COALESCE(?, coindcx_api_secret_enc),
               updated_at             = ?
               WHERE user_id = ?""",
            (telegram_token or None, telegram_chat_id or None,
             coindcx_api_key_enc or None, coindcx_api_secret_enc or None,
             int(time.time()), user_id),
        )
    else:
        await db.execute(
            """INSERT INTO user_settings
               (user_id, telegram_token, telegram_chat_id,
                coindcx_api_key_enc, coindcx_api_secret_enc, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, telegram_token or None, telegram_chat_id or None,
             coindcx_api_key_enc or None, coindcx_api_secret_enc or None,
             int(time.time())),
        )
    await db.commit()


# ── Trigger position queries ──────────────────────────────────────────────────────

async def get_trigger_position(db: aiosqlite.Connection, trigger_id: int) -> Optional[dict]:
    cursor = await db.execute(
        "SELECT * FROM trigger_positions WHERE trigger_id = ?", (trigger_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def upsert_trigger_position(
    db: aiosqlite.Connection,
    trigger_id: int,
    symbol: str,
    coins_held: float,
    avg_entry: float,
    usdt_spent: float,
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
) -> None:
    # Preserve existing SL/TP if not explicitly updated
    if stop_loss_price is None or take_profit_price is None:
        existing = await get_trigger_position(db, trigger_id)
        if existing:
            if stop_loss_price is None:
                stop_loss_price = existing.get("stop_loss_price")
            if take_profit_price is None:
                take_profit_price = existing.get("take_profit_price")

    await db.execute(
        """INSERT OR REPLACE INTO trigger_positions
           (trigger_id, symbol, coins_held, avg_entry, usdt_spent,
            stop_loss_price, take_profit_price, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (trigger_id, symbol.upper(), coins_held, avg_entry, usdt_spent,
         stop_loss_price, take_profit_price, int(time.time())),
    )
    await db.commit()


async def get_open_positions_count(db: aiosqlite.Connection, user_id: int) -> int:
    """Count open (non-zero) positions for all triggers belonging to user_id."""
    cursor = await db.execute(
        """SELECT COUNT(*) FROM trigger_positions tp
           JOIN triggers t ON t.id = tp.trigger_id
           WHERE t.user_id = ? AND tp.coins_held > 1e-8""",
        (user_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def get_positions_for_sl_tp_check(
    db: aiosqlite.Connection, symbol: str
) -> list[dict]:
    """
    Return all open positions for a given symbol that have stop-loss or
    take-profit prices set.  Used by the stream loop to auto-exit positions.
    """
    cursor = await db.execute(
        """SELECT tp.*, t.user_id, t.active
           FROM trigger_positions tp
           JOIN triggers t ON t.id = tp.trigger_id
           WHERE tp.symbol = ?
             AND tp.coins_held > 1e-8
             AND (tp.stop_loss_price IS NOT NULL OR tp.take_profit_price IS NOT NULL)""",
        (symbol.upper(),),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_trigger_position(db: aiosqlite.Connection, trigger_id: int) -> None:
    await db.execute("DELETE FROM trigger_positions WHERE trigger_id = ?", (trigger_id,))
    await db.commit()


# ── Admin queries ─────────────────────────────────────────────────────────────

async def set_admin(db: aiosqlite.Connection, user_id: int, is_admin: bool) -> None:
    await db.execute(
        "UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id)
    )
    await db.commit()


async def list_users(db: aiosqlite.Connection) -> list[dict]:
    """Return all users (password_hash and totp_secret excluded)."""
    cursor = await db.execute(
        "SELECT id, username, email, totp_enabled, is_admin, created_at FROM users"
        " ORDER BY created_at ASC"
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_user(db: aiosqlite.Connection, user_id: int) -> None:
    """
    Delete a user and ALL their associated data in dependency order:
      trade_history → orders → trigger_positions → triggers → user_settings → users
    Signals are global market data (no user_id) — left intact.
    """
    # 1. trade_history rows linked to the user's orders
    await db.execute(
        "DELETE FROM trade_history WHERE order_id IN "
        "(SELECT id FROM orders WHERE user_id = ?)",
        (user_id,),
    )
    # 2. orders placed by this user
    await db.execute("DELETE FROM orders WHERE user_id = ?", (user_id,))
    # 3. trigger_positions for the user's triggers
    await db.execute(
        "DELETE FROM trigger_positions WHERE trigger_id IN "
        "(SELECT id FROM triggers WHERE user_id = ?)",
        (user_id,),
    )
    # 4. triggers
    await db.execute("DELETE FROM triggers WHERE user_id = ?", (user_id,))
    # 5. user settings (encrypted API keys, Telegram creds)
    await db.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
    # 6. the user row itself
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()


async def db_stats(db: aiosqlite.Connection) -> dict:
    """Return row counts for all tables."""
    tables = [
        "users", "signals", "candles", "triggers",
        "orders", "trade_history", "trigger_positions",
    ]
    result = {}
    for table in tables:
        cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
        row = await cursor.fetchone()
        result[table] = row[0] if row else 0
    return result


async def clear_signals(db: aiosqlite.Connection) -> int:
    """Delete all signals. Returns count deleted."""
    cursor = await db.execute("DELETE FROM signals")
    await db.commit()
    return cursor.rowcount


async def clear_all_data(db: aiosqlite.Connection) -> dict:
    """Wipe signals, candles, orders, trade_history, trigger_positions. Keeps users/triggers."""
    counts = {}
    for table in ["signals", "candles", "orders", "trade_history", "trigger_positions"]:
        cursor = await db.execute(f"DELETE FROM {table}")
        counts[table] = cursor.rowcount
    await db.commit()
    return counts
