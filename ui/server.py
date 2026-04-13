"""
FastAPI server for the crypto signal dashboard.

Each WebSocket connection gets its own independent stream + detector,
so multiple browser tabs can watch different symbols/intervals simultaneously.

Dependency injection via app.state (set by orchestrator.py):
    app.state.db            — aiosqlite connection
    app.state.exchange      — CoinDCXClient (for /api/portfolio)
    app.state.telegram_bot  — telegram.Bot (for signal alerts)
    app.state.settings      — Settings

Run standalone (no Telegram/DB):
    python run_ui.py
Run via orchestrator (full stack):
    python orchestrator.py
"""

import asyncio
import json as _json
import logging as _logging
import time as _time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import jwt as _jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from signals.detector import SignalDetector, params_for_interval
from streaming.stream import stream_klines, fetch_historical_klines
from database import queries

SEED_BARS  = 201
static_dir = Path(__file__).parent / "static"
_log       = _logging.getLogger("server")

# ── Auth helpers ───────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def _current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """FastAPI dependency — returns decoded JWT payload or raises 401."""
    token = None
    if credentials:
        token = credentials.credentials
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    from auth.security import decode_jwt
    try:
        return decode_jwt(token)
    except _jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except _jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


async def _get_user_exchange(db, user_id: int):
    """Build a CoinDCXClient from the user's encrypted DB settings."""
    from auth.encryption import safe_decrypt
    from exchange.coindcx import CoinDCXClient
    import aiohttp
    row = await queries.get_user_settings(db, user_id)
    if not row or not row.get("coindcx_api_key_enc"):
        raise HTTPException(status_code=400, detail="CoinDCX API keys not configured in account settings")
    key    = safe_decrypt(row["coindcx_api_key_enc"])
    secret = safe_decrypt(row["coindcx_api_secret_enc"])
    if not key or not secret:
        raise HTTPException(status_code=400, detail="Could not decrypt CoinDCX credentials")
    session = aiohttp.ClientSession()
    return CoinDCXClient(api_key=key, api_secret=secret, session=session), session


# ── Pydantic models ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    email:    str
    password: str

class LoginRequest(BaseModel):
    username:  str
    password:  str
    totp_code: str = ""

class TOTPConfirmRequest(BaseModel):
    username:  str
    totp_code: str

class SettingsUpdate(BaseModel):
    telegram_token:      Optional[str] = None
    telegram_chat_id:    Optional[str] = None
    coindcx_api_key:     Optional[str] = None
    coindcx_api_secret:  Optional[str] = None

class TriggerCreate(BaseModel):
    name:               str
    symbol:             str
    interval:           str
    min_confidence:     str            = "MEDIUM"
    adx_threshold:      Optional[float] = None
    cooldown_bars:      Optional[int]   = None
    trade_amount_usdt:  float           = 10.0   # USDT to allocate per trade

class TriggerBulkDelete(BaseModel):
    ids: list[int]

class TriggerUpdate(BaseModel):
    name:              Optional[str]   = None
    symbol:            Optional[str]   = None
    interval:          Optional[str]   = None
    min_confidence:    Optional[str]   = None
    active:            Optional[bool]  = None
    adx_threshold:     Optional[float] = None
    cooldown_bars:     Optional[int]   = None
    trade_amount_usdt: Optional[float] = None

app = FastAPI()
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


# ── Auth endpoints ─────────────────────────────────────────────────────────────

@app.post("/api/auth/register", status_code=201)
async def api_register(request: Request, body: RegisterRequest):
    """Create a new user account. Returns TOTP provisioning URI for QR code setup."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    if len(body.password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)

    from auth.security import hash_password, generate_totp_secret, get_totp_uri
    existing = await queries.get_user_by_username(db, body.username)
    if existing:
        return JSONResponse({"error": "Username already taken"}, status_code=409)

    totp_secret   = generate_totp_secret()
    password_hash = hash_password(body.password)
    user_id = await queries.create_user(db, body.username, body.email, password_hash, totp_secret)
    totp_uri = get_totp_uri(totp_secret, body.username)

    return {
        "user_id":  user_id,
        "username": body.username,
        "totp_uri": totp_uri,
        "totp_secret": totp_secret,   # for manual entry in authenticator apps
    }


@app.post("/api/auth/totp-confirm")
async def api_totp_confirm(request: Request, body: TOTPConfirmRequest):
    """Confirm the first valid TOTP code to activate 2FA."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)

    from auth.security import verify_totp
    user = await queries.get_user_by_username(db, body.username)
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)
    if not verify_totp(user["totp_secret"], body.totp_code):
        return JSONResponse({"error": "Invalid 2FA code"}, status_code=400)

    await queries.enable_totp(db, user["id"])
    return {"ok": True, "message": "2FA activated successfully"}


@app.post("/api/auth/login")
async def api_login(request: Request, body: LoginRequest):
    """Verify credentials + TOTP, return JWT."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)

    from auth.security import verify_password, verify_totp, create_jwt
    user = await queries.get_user_by_username(db, body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)

    if user["totp_enabled"]:
        if not body.totp_code:
            return JSONResponse({"error": "2FA code required"}, status_code=401)
        if not verify_totp(user["totp_secret"], body.totp_code):
            return JSONResponse({"error": "Invalid 2FA code"}, status_code=401)

    token = create_jwt(user["id"], user["username"])
    return {"token": token, "username": user["username"], "user_id": user["id"]}


@app.get("/api/auth/me")
async def api_me(user: dict = Depends(_current_user)):
    """Validate token and return current user info."""
    return {"user_id": int(user["sub"]), "username": user["username"]}


# ── Profile/Settings endpoints ──────────────────────────────────────────────────

@app.get("/api/profile/settings")
async def api_get_settings(request: Request, user: dict = Depends(_current_user)):
    """Return user settings with API keys masked."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    user_id = int(user["sub"])
    row = await queries.get_user_settings(db, user_id)
    if not row:
        return {"telegram_token": None, "telegram_chat_id": None,
                "has_coindcx_key": False, "has_coindcx_secret": False}
    return {
        "telegram_token":   row.get("telegram_token"),
        "telegram_chat_id": row.get("telegram_chat_id"),
        "has_coindcx_key":  bool(row.get("coindcx_api_key_enc")),
        "has_coindcx_secret": bool(row.get("coindcx_api_secret_enc")),
    }


@app.put("/api/profile/settings")
async def api_update_settings(
    request: Request,
    body: SettingsUpdate,
    user: dict = Depends(_current_user),
):
    """Save settings, test connections, send Telegram confirmation."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)

    import aiohttp
    from auth.encryption import encrypt, safe_decrypt

    user_id = int(user["sub"])
    existing = await queries.get_user_settings(db, user_id)

    # Encrypt new API keys (or keep existing encrypted values)
    key_enc    = encrypt(body.coindcx_api_key)    if body.coindcx_api_key    else (existing or {}).get("coindcx_api_key_enc")
    secret_enc = encrypt(body.coindcx_api_secret) if body.coindcx_api_secret else (existing or {}).get("coindcx_api_secret_enc")

    tg_token   = body.telegram_token    or (existing or {}).get("telegram_token")
    tg_chat_id = body.telegram_chat_id  or (existing or {}).get("telegram_chat_id")

    # Test CoinDCX connection if new keys provided
    if body.coindcx_api_key and body.coindcx_api_secret:
        from exchange.coindcx import CoinDCXClient
        try:
            async with aiohttp.ClientSession() as session:
                client = CoinDCXClient(
                    api_key=body.coindcx_api_key,
                    api_secret=body.coindcx_api_secret,
                    session=session,
                )
                await client.get_balances()
        except Exception as exc:
            return JSONResponse({"error": f"CoinDCX connection failed: {exc}"}, status_code=400)

    # Save to DB
    await queries.upsert_user_settings(
        db, user_id,
        telegram_token=tg_token,
        telegram_chat_id=tg_chat_id,
        coindcx_api_key_enc=key_enc,
        coindcx_api_secret_enc=secret_enc,
    )

    # Send Telegram success notification
    tg_ok = False
    if tg_token and tg_chat_id:
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat_id,
                          "text": "✅ CryptoDash connected successfully! You'll receive signal alerts here."},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
                tg_ok = resp.status == 200
        except Exception as exc:
            _log.warning("Telegram test message failed: %s", exc)

    return {"ok": True, "telegram_notified": tg_ok}


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/portfolio")
async def api_portfolio(request: Request):
    """Proxy to CoinDCX balance endpoint. Returns list of {currency, balance, locked_balance}."""
    exchange = getattr(request.app.state, "exchange", None)
    if exchange is None:
        return JSONResponse({"error": "Exchange client not configured"}, status_code=503)
    try:
        balances = await exchange.get_balances()
        return balances
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/signals/history")
async def api_signals_history(
    request: Request,
    symbol:   Optional[str] = Query(default=None),
    interval: Optional[str] = Query(default=None),
    limit:    int            = Query(default=100),
):
    """Return recent signals from SQLite. All filters optional — omit for all symbols/intervals."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return []
    return await queries.get_recent_signals(db, symbol=symbol, interval=interval, limit=limit)


@app.delete("/api/signals/{signal_id}")
async def api_delete_signal(request: Request, signal_id: int):
    """Hard-delete a single signal by DB id."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    await queries.delete_signal(db, signal_id)
    return {"ok": True}


@app.delete("/api/signals")
async def api_delete_signals(
    request:    Request,
    symbol:     Optional[str] = Query(default=None),
    interval:   Optional[str] = Query(default=None),
    direction:  Optional[str] = Query(default=None),
    confidence: Optional[str] = Query(default=None),
):
    """Bulk-delete signals matching any combination of query filters."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    deleted = await queries.delete_signals(
        db,
        symbol=symbol,
        interval=interval,
        direction=direction,
        confidence=confidence,
    )
    return {"deleted": deleted}


@app.get("/api/triggers")
async def api_get_triggers(request: Request):
    """List ALL triggers (active and inactive)."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return []
    return await queries.get_triggers(db, active_only=False)


@app.post("/api/triggers", status_code=201)
async def api_create_trigger(
    request: Request,
    body: TriggerCreate,
    user: dict = Depends(_current_user),
):
    """Create a new trigger. Checks USDT balance before saving."""
    db       = getattr(request.app.state, "db",           None)
    tg_bot   = getattr(request.app.state, "telegram_bot", None)
    settings = getattr(request.app.state, "settings",     None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)

    user_id = int(user["sub"])
    sym  = body.symbol.upper()
    iv   = body.interval
    conf = body.min_confidence.upper()

    # Balance check against user's CoinDCX account
    try:
        exchange, session = await _get_user_exchange(db, user_id)
        async with session:
            balances = await exchange.get_balances()
        usdt_row = next((b for b in balances if b.get("currency") == "USDT"), None)
        usdt_bal = float(usdt_row.get("balance", 0)) if usdt_row else 0.0
        if usdt_bal < body.trade_amount_usdt:
            return JSONResponse(
                {"error": f"Insufficient USDT balance: {usdt_bal:.2f} available, {body.trade_amount_usdt:.2f} required"},
                status_code=400,
            )
    except HTTPException:
        # No CoinDCX configured yet — allow trigger creation without balance check
        pass

    tid = await queries.create_trigger(
        db, sym, iv, conf,
        adx_threshold=body.adx_threshold,
        cooldown_bars=body.cooldown_bars,
        name=body.name,
        trade_amount_usdt=body.trade_amount_usdt,
        user_id=user_id,
    )
    if tg_bot and settings:
        from telegram_bot.alerts import send_trigger_alert
        await send_trigger_alert(tg_bot, settings.telegram_chat_id, "created", sym, iv, conf)
    return {
        "id": tid, "name": body.name, "symbol": sym, "interval": iv,
        "min_confidence": conf, "active": True,
        "adx_threshold": body.adx_threshold,
        "cooldown_bars": body.cooldown_bars,
        "trade_amount_usdt": body.trade_amount_usdt,
    }


@app.put("/api/triggers/{trigger_id}")
async def api_update_trigger(
    request: Request,
    trigger_id: int,
    body: TriggerUpdate,
    user: dict = Depends(_current_user),
):
    """Update an existing trigger."""
    db       = getattr(request.app.state, "db",           None)
    tg_bot   = getattr(request.app.state, "telegram_bot", None)
    settings = getattr(request.app.state, "settings",     None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    trig = await queries.get_trigger(db, trigger_id)
    await queries.update_trigger(
        db, trigger_id,
        symbol=body.symbol, interval=body.interval,
        min_confidence=body.min_confidence, active=body.active,
        adx_threshold=body.adx_threshold,
        cooldown_bars=body.cooldown_bars,
        name=body.name,
        trade_amount_usdt=body.trade_amount_usdt,
    )
    if tg_bot and settings and trig:
        from telegram_bot.alerts import send_trigger_alert
        only_toggle = (body.active is not None
                       and body.symbol is None
                       and body.interval is None
                       and body.min_confidence is None)
        action = ("enabled" if body.active else "disabled") if only_toggle else "updated"
        sym  = (body.symbol  or trig["symbol"]).upper()
        iv   = body.interval or trig["interval"]
        conf = (body.min_confidence or trig["min_confidence"]).upper()
        await send_trigger_alert(tg_bot, settings.telegram_chat_id, action, sym, iv, conf)
    return {"ok": True}


@app.post("/api/triggers/bulk-delete")
async def api_bulk_delete_triggers(request: Request, body: TriggerBulkDelete):
    """Delete multiple triggers in one request."""
    db       = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    for tid in body.ids:
        await queries.delete_trigger(db, tid)
    return {"deleted": len(body.ids)}


@app.delete("/api/triggers/{trigger_id}")
async def api_delete_trigger(
    request: Request,
    trigger_id: int,
    delete_signals: bool = Query(default=False),
):
    """Delete a trigger. Pass delete_signals=true to also purge its associated signals."""
    db       = getattr(request.app.state, "db",           None)
    tg_bot   = getattr(request.app.state, "telegram_bot", None)
    settings = getattr(request.app.state, "settings",     None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    trig = await queries.get_trigger(db, trigger_id)
    signals_deleted = 0
    if delete_signals and trig:
        signals_deleted = await queries.delete_signals(
            db, symbol=trig["symbol"], interval=trig["interval"]
        )
    await queries.delete_trigger(db, trigger_id)
    if tg_bot and settings and trig:
        from telegram_bot.alerts import send_trigger_alert
        await send_trigger_alert(
            tg_bot, settings.telegram_chat_id, "deleted",
            trig["symbol"], trig["interval"], trig["min_confidence"],
        )
    return {"ok": True, "signals_deleted": signals_deleted}


# ── Analytics endpoint ─────────────────────────────────────────────────────────

_PERIOD_SECS = {"1h": 3600, "4h": 14400, "1d": 86400, "7d": 604800, "30d": 2592000}


def _simulate_portfolio(
    signals: list[dict],
    initial_usdt: float,
    buy_pct: float,    # used as fallback base before Kelly has enough data
    sell_pct: float,   # unused directly — sell ratio comes from adaptive engine
) -> dict:
    """
    Adaptive walk-forward portfolio simulation.

    Uses Half-Kelly Criterion with live performance feedback:
    - Buy size = engine.buy_pct_for(confidence, adx_val) % of USDT
    - Sell ratio = engine.sell_ratio_for(confidence)
    - Engine updates after each completed BUY→SELL cycle

    DCA rules (same as before):
    - Max 3 DCA entries per (symbol, interval) stream
    - Entry 1: full Kelly size, Entry 2: half, Entry 3: quarter
    """
    from signals.adaptive_strategy import AdaptiveState

    _MAX_DCA   = 3
    _DCA_SCALE = [1.0, 0.5, 0.25]

    # Local walk-forward engine (starts fresh for simulation)
    sim_engine = AdaptiveState()

    usdt: float = initial_usdt
    stream_coins:   dict = {}   # (sym, iv) → coins held
    stream_entries: dict = {}   # (sym, iv) → DCA entry count
    stream_avg:     dict = {}   # (sym, iv) → avg entry price
    last_price:     dict = {}   # sym → last price
    holdings:       dict = {}   # base → total coins across all streams

    # Track completed trades for engine updates
    completed_for_engine: list = []

    sim_trades:   list = []
    skipped_buys:  int = 0
    skipped_sells: int = 0
    portfolio_val  = initial_usdt

    for sig in signals:  # oldest-first
        sym      = sig["symbol"]
        interval = sig["interval"]
        price    = sig["entry_price"]
        conf     = sig["confidence"]
        adx_val  = sig.get("adx_val")
        stream   = (sym, interval)
        last_price[sym] = price

        base = sym
        for quote in ("USDT", "BTC", "ETH", "BNB", "INR"):
            if sym.endswith(quote):
                base = sym[:-len(quote)]
                break

        entries    = stream_entries.get(stream, 0)
        coins_held = stream_coins.get(stream, 0.0)

        if sig["direction"] == "BUY":
            if entries >= _MAX_DCA:
                skipped_buys += 1
                continue
            # Adaptive sizing: engine.buy_pct_for × DCA scale
            adaptive_pct = sim_engine.buy_pct_for(conf, adx_val)
            scale   = _DCA_SCALE[entries]
            spend   = usdt * (adaptive_pct / 100.0) * scale
            if spend < 0.0001 or usdt < spend:
                skipped_buys += 1
                continue
            coins = spend / price
            usdt -= spend
            stream_entries[stream] = entries + 1
            stream_coins[stream]   = coins_held + coins
            holdings[base] = holdings.get(base, 0.0) + coins
            prev_avg = stream_avg.get(stream, 0.0)
            stream_avg[stream] = (
                (prev_avg * coins_held + price * coins) / (coins_held + coins)
                if coins_held + coins > 0 else price
            )
            portfolio_val = usdt + sum(
                holdings.get(b, 0) * last_price.get(b + "USDT", last_price.get(sym, price))
                for b in holdings
            )
            sim_trades.append({
                "time":          sig["open_time"] // 1000,
                "symbol":        sym,
                "interval":      interval,
                "direction":     "BUY",
                "confidence":    conf,
                "adx_val":       round(adx_val, 1) if adx_val else None,
                "dca_entry":     entries + 1,
                "price":         round(price, 4),
                "avg_entry":     None,
                "adaptive_pct":  adaptive_pct,
                "usdt_spent":    round(spend, 4),
                "coins_bought":  round(coins, 8),
                "usdt_balance":  round(usdt, 4),
                "portfolio_val": round(portfolio_val, 4),
            })

        elif sig["direction"] == "SELL":
            if coins_held < 1e-10:
                skipped_sells += 1
                continue
            ratio      = sim_engine.sell_ratio_for(conf)
            coins_sold = coins_held * ratio
            received   = coins_sold * price
            avg_entry  = stream_avg.get(stream, price)
            pnl_pct    = round((price - avg_entry) / avg_entry * 100, 6) if avg_entry else 0.0

            usdt += received
            stream_coins[stream] = coins_held - coins_sold
            holdings[base] = max(0.0, holdings.get(base, 0.0) - coins_sold)

            if ratio >= 1.0 or stream_coins[stream] < 1e-10:
                stream_entries[stream] = 0
                stream_avg[stream]     = 0.0
                stream_coins[stream]   = 0.0
            else:
                stream_entries[stream] = max(0, round(entries * (1.0 - ratio)))

            portfolio_val = usdt + sum(
                holdings.get(b, 0) * last_price.get(b + "USDT", last_price.get(sym, price))
                for b in holdings
            )
            sim_trades.append({
                "time":          sig["open_time"] // 1000,
                "symbol":        sym,
                "interval":      interval,
                "direction":     "SELL",
                "confidence":    conf,
                "adx_val":       round(adx_val, 1) if adx_val else None,
                "exit_pct":      round(ratio * 100),
                "price":         round(price, 4),
                "avg_entry":     round(avg_entry, 4),
                "usdt_received": round(received, 4),
                "coins_sold":    round(coins_sold, 8),
                "pnl_pct":       round(pnl_pct, 3),
                "usdt_balance":  round(usdt, 4),
                "portfolio_val": round(portfolio_val, 4),
            })

            # Feed completed trade into walk-forward engine
            completed_for_engine.append({
                "pnl_pct":       pnl_pct,
                "portfolio_val": portfolio_val,
            })
            if len(completed_for_engine) >= 5:
                sim_engine.update(completed_for_engine)

    holding_value = sum(
        holdings.get(b, 0) * last_price.get(b + "USDT", 0) for b in holdings
    )
    final_value  = usdt + holding_value
    total_return = (final_value - initial_usdt) / initial_usdt * 100

    return {
        "initial_usdt":       round(initial_usdt, 2),
        "final_cash_usdt":    round(usdt, 4),
        "final_holding_usdt": round(holding_value, 4),
        "final_value_usdt":   round(final_value, 4),
        "total_return_pct":   round(total_return, 3),
        "holdings":           {k: round(v, 8) for k, v in holdings.items() if v > 1e-10},
        "skipped_buys":       skipped_buys,
        "skipped_sells":      skipped_sells,
        "sim_trades":         sim_trades[-100:],
        "sim_trade_count":    len(sim_trades),
        "engine_state":       sim_engine.summary(),
    }


def _pair_signals(signals: list[dict]) -> tuple[list[dict], Optional[dict]]:
    """
    Pair consecutive direction-flipping signals into completed trades.
    Returns (trades_list, open_position_or_None).
    Same-direction consecutive signals keep the FIRST entry (original entry price).
    """
    trades = []
    open_trade: Optional[dict] = None

    for sig in signals:  # already sorted oldest-first
        if open_trade is None:
            open_trade = sig
        elif open_trade["direction"] != sig["direction"]:
            if open_trade["direction"] == "BUY":
                pnl_pct = (sig["entry_price"] - open_trade["entry_price"]) / open_trade["entry_price"] * 100
            else:  # SELL → BUY short
                pnl_pct = (open_trade["entry_price"] - sig["entry_price"]) / open_trade["entry_price"] * 100

            trades.append({
                "open_time":   open_trade["open_time"] // 1000,
                "close_time":  sig["open_time"] // 1000,
                "symbol":      open_trade["symbol"],
                "interval":    open_trade["interval"],
                "direction":   open_trade["direction"],
                "entry_price": open_trade["entry_price"],
                "exit_price":  sig["entry_price"],
                "pnl_pct":     round(pnl_pct, 6),
                "won":         pnl_pct > 0,
                "confidence":  open_trade["confidence"],
            })
            open_trade = sig
        # else: same direction — keep first entry, discard duplicate

    return trades, open_trade


@app.get("/api/analytics")
async def api_analytics(
    request:      Request,
    symbol:       str   = Query(default="ALL"),
    interval:     str   = Query(default="ALL"),
    confidence:   str   = Query(default="ALL"),
    period:       str   = Query(default="1d"),
    initial_usdt: float = Query(default=100.0),
    buy_pct:      float = Query(default=10.0),
    sell_pct:     float = Query(default=100.0),
):
    """
    Signal performance analytics: pairs BUY→SELL (and SELL→BUY) signals into
    trades and computes win-rate, P&L, and breakdowns by symbol and confidence.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)

    now   = int(_time.time())
    since = now - _PERIOD_SECS[period] if period in _PERIOD_SECS else None

    signals = await queries.get_signals_for_analytics(
        db,
        symbol     = None if symbol     == "ALL" else symbol,
        interval   = None if interval   == "ALL" else interval,
        confidence = None if confidence == "ALL" else confidence,
        since      = since,
    )

    # Group per (symbol, interval) then pair within each group
    groups: dict[tuple, list] = defaultdict(list)
    for sig in signals:
        groups[(sig["symbol"], sig["interval"])].append(sig)

    all_trades: list[dict] = []
    open_positions: list[dict] = []

    for (sym, iv), grp in groups.items():
        trades, open_pos = _pair_signals(grp)
        all_trades.extend(trades)
        if open_pos:
            open_positions.append({
                "symbol":      sym,
                "interval":    iv,
                "direction":   open_pos["direction"],
                "entry_price": open_pos["entry_price"],
                "confidence":  open_pos["confidence"],
                "open_time":   open_pos["open_time"] // 1000,
            })

    all_trades.sort(key=lambda t: t["close_time"])

    wins   = [t for t in all_trades if t["pnl_pct"] > 0]
    losses = [t for t in all_trades if t["pnl_pct"] <= 0]
    n      = len(all_trades)

    def _stats(subset: list[dict]) -> dict:
        if not subset:
            return {"trades": 0, "wins": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0}
        w = [t for t in subset if t["pnl_pct"] > 0]
        return {
            "trades":    len(subset),
            "wins":      len(w),
            "win_rate":  round(len(w) / len(subset) * 100, 1),
            "total_pnl": round(sum(t["pnl_pct"] for t in subset), 6),
            "avg_pnl":   round(sum(t["pnl_pct"] for t in subset) / len(subset), 6),
        }

    by_confidence = {
        conf: _stats([t for t in all_trades if t["confidence"] == conf])
        for conf in ("HIGH", "MEDIUM", "LOW")
        if any(t["confidence"] == conf for t in all_trades)
    }

    by_symbol = {
        sym: _stats([t for t in all_trades if t["symbol"] == sym])
        for sym in sorted({t["symbol"] for t in all_trades})
    }

    all_symbols = sorted({s["symbol"] for s in signals})

    return {
        "total_signals":  len(signals),
        "total_trades":   n,
        "all_symbols":    all_symbols,
        "open_positions": open_positions,
        "win_rate":       round(len(wins) / n * 100, 1) if n else 0,
        "avg_gain_pct":   round(sum(t["pnl_pct"] for t in wins)   / len(wins),   6) if wins   else None,
        "avg_loss_pct":   round(sum(t["pnl_pct"] for t in losses) / len(losses), 6) if losses else None,
        "total_pnl_pct":  round(sum(t["pnl_pct"] for t in all_trades), 6),
        "best_trade_pct": round(max((t["pnl_pct"] for t in all_trades), default=0), 6),
        "worst_trade_pct":round(min((t["pnl_pct"] for t in all_trades), default=0), 6),
        "by_confidence":  by_confidence,
        "by_symbol":      by_symbol,
        "trades":         all_trades[-100:],   # most recent 100 trades
        "simulation":     _simulate_portfolio(signals, initial_usdt, buy_pct, sell_pct),
    }


@app.get("/api/adaptive")
async def api_adaptive_state(request: Request):
    """Return current adaptive engine state and position-size recommendations."""
    from signals.adaptive_strategy import engine as _adaptive_engine
    return _adaptive_engine.summary()


@app.on_event("startup")
async def _on_startup():
    """
    Auto-init DB if not already injected by orchestrator (covers run_ui.py standalone mode).
    Then restore adaptive engine state and start the background monitor.
    """
    import asyncio, os
    # Standalone mode: no orchestrator set app.state.db — initialise it now
    if getattr(app.state, "db", None) is None:
        from database.db import init_db
        db_path = os.environ.get("DB_PATH", "data/trading.db")
        app.state.db = await init_db(db_path)
    asyncio.create_task(_load_adaptive_state_when_ready())


async def _load_adaptive_state_when_ready() -> None:
    """Wait for DB to be available then restore adaptive engine state."""
    import asyncio
    import logging
    from signals.adaptive_strategy import engine as _adaptive_engine
    for _ in range(30):   # retry for up to 30 seconds
        await asyncio.sleep(1)
        db = getattr(app.state, "db", None)
        if db is None:
            continue
        try:
            saved = await queries.load_adaptive_state(db)
            if saved:
                _adaptive_engine.from_dict(saved)
                logging.getLogger("adaptive_monitor").info(
                    "Adaptive engine restored: WR=%.0f%%  Kelly=%.1f%%",
                    _adaptive_engine.win_rate * 100,
                    _adaptive_engine.kelly_fraction() * 100,
                )
        except Exception:
            pass
        asyncio.create_task(_adaptive_monitor_loop())
        return


# ── Auto-trade execution ───────────────────────────────────────────────────────

_engine_lock = asyncio.Lock()


async def _execute_trigger_trade(db, trigger: dict, signal, symbol: str, interval: str, signal_id) -> None:
    """
    Execute a market order on CoinDCX for a matched trigger.
    BUY  → spend trigger.trade_amount_usdt to buy base asset.
    SELL → sell all coins held by this trigger's position.
    Updates trigger_positions and feeds the adaptive engine with real P&L.
    """
    import aiohttp
    from auth.encryption import safe_decrypt
    from exchange.coindcx import CoinDCXClient
    from signals.adaptive_strategy import engine as _adaptive_engine

    user_id    = trigger.get("user_id")
    trigger_id = trigger["id"]
    amount     = trigger.get("trade_amount_usdt") or 0.0

    if not user_id or not amount:
        return  # legacy trigger without user or amount — skip auto-trade

    user_settings = await queries.get_user_settings(db, user_id)
    if not user_settings:
        return

    key    = safe_decrypt(user_settings.get("coindcx_api_key_enc"))
    secret = safe_decrypt(user_settings.get("coindcx_api_secret_enc"))
    if not key or not secret:
        return

    try:
        async with aiohttp.ClientSession() as session:
            exchange = CoinDCXClient(api_key=key, api_secret=secret, session=session)

            if signal.direction == "BUY":
                # Check current USDT balance
                balances = await exchange.get_balances()
                usdt_row = next((b for b in balances if b.get("currency") == "USDT"), None)
                usdt_bal = float(usdt_row.get("balance", 0)) if usdt_row else 0.0
                if usdt_bal < amount:
                    _log.warning("Trigger %d: insufficient USDT %.2f < %.2f", trigger_id, usdt_bal, amount)
                    return

                qty = round(amount / signal.entry_price, 8)
                result = await exchange.create_order(
                    side="buy", market=symbol,
                    order_type="market_order", total_quantity=qty,
                )
                cdx_order_id = result.get("id") or result.get("order_id")

                # Record order
                order_id = await queries.insert_order(
                    db, symbol, "buy", "market_order", qty,
                    price=signal.entry_price, signal_id=signal_id,
                )
                await queries.update_order_status(db, order_id, "filled", cdx_order_id)

                # Update trigger position (DCA-style: blend avg entry)
                pos = await queries.get_trigger_position(db, trigger_id)
                if pos and pos["coins_held"] > 0:
                    total_coins = pos["coins_held"] + qty
                    new_avg = (pos["avg_entry"] * pos["coins_held"] + signal.entry_price * qty) / total_coins
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, total_coins, new_avg, pos["usdt_spent"] + amount
                    )
                else:
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, qty, signal.entry_price, amount
                    )
                _log.info("Trigger %d BUY executed: %.8f %s @ %.4f", trigger_id, qty, symbol, signal.entry_price)

            elif signal.direction == "SELL":
                pos = await queries.get_trigger_position(db, trigger_id)
                if not pos or pos["coins_held"] < 1e-8:
                    return  # nothing to sell

                coins_to_sell = pos["coins_held"]
                result = await exchange.create_order(
                    side="sell", market=symbol,
                    order_type="market_order", total_quantity=round(coins_to_sell, 8),
                )
                cdx_order_id = result.get("id") or result.get("order_id")
                avg_entry    = pos["avg_entry"]
                pnl_pct      = (signal.entry_price - avg_entry) / avg_entry * 100 if avg_entry else 0.0

                # Record order + trade fill
                order_id = await queries.insert_order(
                    db, symbol, "sell", "market_order", coins_to_sell,
                    price=signal.entry_price, signal_id=signal_id,
                )
                await queries.update_order_status(db, order_id, "filled", cdx_order_id)
                await queries.insert_trade(
                    db, order_id, symbol, "sell",
                    coins_to_sell, signal.entry_price, pnl=pnl_pct,
                )

                # Zero out position
                await queries.upsert_trigger_position(db, trigger_id, symbol, 0.0, 0.0, 0.0)

                # Fetch real portfolio value for accurate adaptive engine update
                try:
                    balances_after = await exchange.get_balances()
                    portfolio_val = sum(float(b.get("balance", 0)) for b in balances_after if b.get("currency") == "USDT")
                except Exception:
                    portfolio_val = _adaptive_engine.peak_portfolio

                # Feed real trade into adaptive engine immediately
                async with _engine_lock:
                    _adaptive_engine.update([{"pnl_pct": pnl_pct, "portfolio_val": portfolio_val}])
                await queries.save_adaptive_state(db, _adaptive_engine.to_dict())

                _log.info(
                    "Trigger %d SELL executed: %.8f %s @ %.4f  PnL=%.4f%%",
                    trigger_id, coins_to_sell, symbol, signal.entry_price, pnl_pct,
                )

    except Exception as exc:
        _log.error("Auto-trade failed for trigger %d: %s", trigger_id, exc)


async def _adaptive_monitor_loop() -> None:
    """
    Background task: every 5 min, read recent signals, pair into trades,
    update the adaptive engine, persist to DB.
    """
    import asyncio
    import logging
    from collections import defaultdict
    from signals.adaptive_strategy import engine as _adaptive_engine
    log = logging.getLogger("adaptive_monitor")

    while True:
        await asyncio.sleep(300)   # 5-minute cycle
        db = getattr(app.state, "db", None)
        if db is None:
            continue
        try:
            signals = await queries.get_signals_for_analytics(db)
            if not signals:
                continue

            groups: dict = defaultdict(list)
            for sig in signals:
                groups[(sig["symbol"], sig["interval"])].append(sig)

            all_trades = []
            for grp in groups.values():
                trades, _ = _pair_signals(grp)
                all_trades.extend(trades)

            if not all_trades:
                continue

            all_trades.sort(key=lambda t: t["close_time"])

            # Enrich with running portfolio value (starts at 100 for relative calc)
            running = 100.0
            for t in all_trades:
                running = running * (1 + t["pnl_pct"] / 100)
                t["portfolio_val"] = running

            async with _engine_lock:
                _adaptive_engine.update(all_trades)
            await queries.save_adaptive_state(db, _adaptive_engine.to_dict())
            log.info(
                "Adaptive engine: %d trades  WR=%.0f%%  Kelly=%.1f%%  DD=%.1f%%  perf=%.2f  CB=%s",
                len(all_trades),
                _adaptive_engine.win_rate * 100,
                _adaptive_engine.kelly_fraction() * 100,
                _adaptive_engine.current_drawdown_pct,
                _adaptive_engine.perf_multiplier,
                _adaptive_engine.consecutive_losses >= 3 or _adaptive_engine.current_drawdown_pct >= 10,
            )
        except Exception as exc:
            log.error("Adaptive monitor error: %s", exc)


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    symbol:    str            = Query(default="ethusdt"),
    interval:  str            = Query(default="1m"),
    adx_min:   Optional[float] = Query(default=None),
    min_conf:  Optional[int]   = Query(default=None),
    cooldown:  Optional[int]   = Query(default=None),
):
    await websocket.accept()
    task = asyncio.create_task(
        _connection_loop(websocket, symbol.lower(), interval, websocket.app,
                         adx_min=adx_min, min_conf=min_conf, cooldown=cooldown)
    )
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _connection_loop(
    ws: WebSocket, symbol: str, interval: str, app_state, *,
    adx_min: Optional[float] = None,
    min_conf: Optional[int]  = None,
    cooldown: Optional[int]  = None,
) -> None:
    """Fetch history, send it, then stream live candles + signals to one client."""
    db       = getattr(app_state.state, "db",           None)
    tg_bot   = getattr(app_state.state, "telegram_bot", None)
    settings = getattr(app_state.state, "settings",     None)

    # Start from tier defaults, then apply any manual overrides from the UI
    det_params = params_for_interval(interval)
    if adx_min  is not None: det_params["adx_threshold"]     = adx_min
    if min_conf is not None: det_params["min_confirmations"] = min_conf
    if cooldown is not None: det_params["cooldown_bars"]     = cooldown
    detector = SignalDetector(**det_params)
    try:
        await ws.send_json({"type": "meta", "symbol": symbol.upper(), "interval": interval})

        historical = await fetch_historical_klines(symbol, interval, limit=SEED_BARS)
        detector.seed(historical)

        for snap in detector.history_snapshots():
            await ws.send_json({"type": "candle", **asdict(snap)})

        await ws.send_json({"type": "ready"})

        async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=False):
            if kline.is_closed:
                signal = detector.update(kline)
                snap   = detector.current_snapshot()
                await ws.send_json({"type": "candle", **asdict(snap)})

                # Persist closed candle
                if db is not None:
                    try:
                        await queries.insert_candle(db, symbol, interval, kline)
                    except Exception:
                        pass

                if signal:
                    # Persist signal to DB first so we have the id
                    signal_id: Optional[int] = None
                    if db is not None:
                        try:
                            signal_id = await queries.insert_signal(db, symbol, interval, signal)
                        except Exception:
                            pass

                    # Check which active triggers match this signal
                    matched_triggers: list[dict] = []
                    if db is not None:
                        try:
                            active_triggers = await queries.get_triggers(db)
                            matched_triggers = [
                                t for t in active_triggers
                                if queries.trigger_matches(
                                    t, symbol, interval, signal.confidence, signal.adx_val
                                )
                            ]
                        except Exception:
                            pass
                    trigger_matched = bool(matched_triggers)
                    trigger_names   = [
                        t.get("name") or f"{t['symbol']} {t['interval']}"
                        for t in matched_triggers
                    ]

                    from signals.adaptive_strategy import engine as _adaptive_engine
                    rec_buy_pct = _adaptive_engine.buy_pct_for(signal.confidence, signal.adx_val)

                    signal_payload = {
                        "type":            "signal",
                        "id":              signal_id,
                        "symbol":          symbol.upper(),
                        "interval":        interval,
                        "direction":       signal.direction,
                        "confidence":      signal.confidence,
                        "entry_price":     signal.entry_price,
                        "time":            signal.open_time // 1000,
                        "macd_val":        signal.macd_val,
                        "signal_val":      signal.signal_val,
                        "histogram":       signal.histogram,
                        "adx_val":         signal.adx_val,
                        "reasons":         signal.reasons,
                        "trend_note":      signal.trend_note,
                        "trigger_matched": trigger_matched,
                        "trigger_names":   trigger_names,
                        "rec_buy_pct":     rec_buy_pct,
                    }
                    await ws.send_json(signal_payload)

                    # Send Telegram alert and execute auto-trades for each matched trigger
                    if trigger_matched:
                        if tg_bot is not None and settings is not None:
                            try:
                                from telegram_bot.alerts import send_signal_alert
                                await send_signal_alert(
                                    bot=tg_bot,
                                    chat_id=settings.telegram_chat_id,
                                    signal=signal,
                                    symbol=symbol.upper(),
                                    interval=interval,
                                    phase=settings.trading_phase,
                                    db=db,
                                    exchange=getattr(app_state.state, "exchange", None),
                                    settings=settings,
                                    signal_id=signal_id,
                                )
                            except Exception:
                                pass
                        # Fire-and-forget auto-trade tasks per matched trigger
                        if db is not None:
                            for trig in matched_triggers:
                                asyncio.create_task(
                                    _execute_trigger_trade(
                                        db, trig, signal, symbol.upper(), interval, signal_id
                                    )
                                )
            else:
                await ws.send_json({
                    "type":   "live",
                    "time":   kline.open_time // 1000,
                    "open":   kline.open,
                    "high":   kline.high,
                    "low":    kline.low,
                    "close":  kline.close,
                    "volume": kline.volume,
                })
    except Exception:
        pass
