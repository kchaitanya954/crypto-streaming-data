"""
SQLite database initialisation.

Creates all tables if they don't exist and returns an open aiosqlite connection.
The caller owns the connection lifetime — call await db.close() on shutdown.
"""

import os
import aiosqlite


_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS candles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT    NOT NULL,
    interval  TEXT    NOT NULL,
    open_time INTEGER NOT NULL,
    open      REAL    NOT NULL,
    high      REAL    NOT NULL,
    low       REAL    NOT NULL,
    close     REAL    NOT NULL,
    volume    REAL    NOT NULL,
    UNIQUE(symbol, interval, open_time)
);
CREATE INDEX IF NOT EXISTS idx_candles ON candles(symbol, interval, open_time DESC);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT    NOT NULL,
    interval    TEXT    NOT NULL,
    open_time   INTEGER NOT NULL,
    direction   TEXT    NOT NULL,
    confidence  TEXT    NOT NULL,
    entry_price REAL    NOT NULL,
    macd_val    REAL,
    signal_val  REAL,
    histogram   REAL,
    adx_val     REAL,
    trend_note  TEXT,
    reasons     TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals ON signals(symbol, interval, created_at DESC);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id        INTEGER REFERENCES signals(id),
    coindcx_order_id TEXT,
    symbol           TEXT    NOT NULL,
    side             TEXT    NOT NULL,
    order_type       TEXT    NOT NULL,
    quantity         REAL    NOT NULL,
    price            REAL,
    status           TEXT    NOT NULL DEFAULT 'pending',
    telegram_msg_id  INTEGER,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER REFERENCES orders(id),
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,
    filled_qty   REAL    NOT NULL,
    filled_price REAL    NOT NULL,
    fee          REAL,
    fee_currency TEXT,
    pnl          REAL,
    created_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS triggers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT    NOT NULL,
    interval       TEXT    NOT NULL,
    min_confidence TEXT    NOT NULL DEFAULT 'MEDIUM',
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS adaptive_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    state_json TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    totp_secret   TEXT    NOT NULL,
    totp_enabled  INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id                INTEGER PRIMARY KEY REFERENCES users(id),
    telegram_token         TEXT,
    telegram_chat_id       TEXT,
    coindcx_api_key_enc    TEXT,
    coindcx_api_secret_enc TEXT,
    updated_at             INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS trigger_positions (
    trigger_id  INTEGER PRIMARY KEY REFERENCES triggers(id),
    symbol      TEXT    NOT NULL,
    coins_held  REAL    NOT NULL DEFAULT 0,
    avg_entry   REAL    NOT NULL DEFAULT 0,
    usdt_spent  REAL    NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
);
"""


async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open (or create) the SQLite database and run schema migrations."""
    os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    await db.commit()
    # Safe migrations — add new columns if they don't exist yet
    for col, defn in [
        ("adx_threshold",     "REAL"),
        ("cooldown_bars",     "INTEGER"),
        ("name",              "TEXT"),
        ("trade_amount_usdt", "REAL"),
        ("user_id",           "INTEGER"),
    ]:
        try:
            await db.execute(f"ALTER TABLE triggers ADD COLUMN {col} {defn}")
            await db.commit()
        except Exception:
            pass  # column already exists
    return db
