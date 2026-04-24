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


async def _get_user_exchange(db, user_id: int, request: Request = None):
    """
    Build a CoinDCXClient using only the logged-in user's own encrypted DB keys.
    Never falls back to any global keys — each user must configure their own.
    """
    from auth.encryption import safe_decrypt
    from exchange.coindcx_client import CoinDCXClient
    import aiohttp

    row = await queries.get_user_settings(db, user_id)
    if not row or not row.get("coindcx_api_key_enc"):
        raise HTTPException(
            status_code=400,
            detail="CoinDCX API keys not configured. Go to ⚙ Account Settings and save your API Key and Secret.",
        )
    key    = safe_decrypt(row["coindcx_api_key_enc"])
    secret = safe_decrypt(row["coindcx_api_secret_enc"])
    if not key or not secret:
        raise HTTPException(
            status_code=400,
            detail="CoinDCX credentials could not be decrypted — please re-enter them in ⚙ Account Settings.",
        )
    session = aiohttp.ClientSession()
    return CoinDCXClient(api_key=key, api_secret=secret, session=session), session


# ── Pydantic models ────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username:         str
    email:            str
    password:         str
    confirm_password: str = ""   # optional — validated if provided

class ChangePasswordRequest(BaseModel):
    current_password:  str
    new_password:      str
    confirm_password:  str

class ForgotPasswordRequest(BaseModel):
    email: str

class ResetPasswordRequest(BaseModel):
    token:            str
    new_password:     str
    confirm_password: str

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
    trade_amount_usdt:  float           = 10.0
    market_type:        str            = "spot"     # "spot" | "futures"
    leverage:           int            = 1          # 1–20 (futures only)

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
    market_type:       Optional[str]   = None
    leverage:          Optional[int]   = None

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
    if body.confirm_password and body.password != body.confirm_password:
        return JSONResponse({"error": "Passwords do not match"}, status_code=400)

    from auth.security import hash_password, generate_totp_secret, get_totp_uri
    existing = await queries.get_user_by_username(db, body.username)
    if existing:
        return JSONResponse({"error": "Username already taken"}, status_code=409)

    totp_secret   = generate_totp_secret()
    password_hash = hash_password(body.password)
    user_id = await queries.create_user(db, body.username, body.email, password_hash, totp_secret)

    # First user ever becomes admin automatically
    all_users = await queries.list_users(db)
    if len(all_users) == 1:
        await queries.set_admin(db, user_id, True)

    totp_uri = get_totp_uri(totp_secret, body.username)

    # Generate QR code server-side as SVG data URL (no Pillow required)
    import qrcode
    import qrcode.image.svg
    import io
    import base64
    factory = qrcode.image.svg.SvgPathImage
    qr_img  = qrcode.make(totp_uri, image_factory=factory)
    buf = io.BytesIO()
    qr_img.save(buf)
    qr_data_url = "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()

    return {
        "user_id":     user_id,
        "username":    body.username,
        "totp_uri":    totp_uri,
        "totp_secret": totp_secret,   # for manual entry in authenticator apps
        "qr_data_url": qr_data_url,
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

    token = create_jwt(user["id"], user["username"], is_admin=bool(user.get("is_admin")))
    return {"token": token, "username": user["username"], "user_id": user["id"],
            "is_admin": bool(user.get("is_admin"))}


@app.get("/api/auth/me")
async def api_me(request: Request, user: dict = Depends(_current_user)):
    """
    Validate token and return current user info.
    Rejects tokens issued before this server process started — forces re-login
    after a Docker rebuild / restart so stale localStorage sessions are cleared.
    """
    startup_time = getattr(request.app.state, "startup_time", 0)
    token_iat    = int(user.get("iat", 0))
    if startup_time and token_iat < startup_time:
        raise HTTPException(
            status_code=401,
            detail="Session expired — server was restarted, please log in again",
        )
    return {
        "user_id":  int(user["sub"]),
        "username": user["username"],
        "is_admin": bool(user.get("is_admin")),
    }


@app.post("/api/auth/change-password")
async def api_change_password(
    request: Request,
    body: ChangePasswordRequest,
    user: dict = Depends(_current_user),
):
    """Change password for the currently logged-in user."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    if len(body.new_password) < 8:
        return JSONResponse({"error": "New password must be at least 8 characters"}, status_code=400)
    if body.new_password != body.confirm_password:
        return JSONResponse({"error": "New passwords do not match"}, status_code=400)

    from auth.security import verify_password, hash_password
    user_row = await queries.get_user_by_id(db, int(user["sub"]))
    if not user_row or not verify_password(body.current_password, user_row["password_hash"]):
        return JSONResponse({"error": "Current password is incorrect"}, status_code=401)

    new_hash = hash_password(body.new_password)
    await queries.update_password(db, int(user["sub"]), new_hash)
    return {"ok": True, "message": "Password updated successfully"}


@app.post("/api/auth/forgot-password")
async def api_forgot_password(request: Request, body: ForgotPasswordRequest):
    """
    Generate a password-reset token and email it to the user.
    Silently succeeds even if email not found (prevents user enumeration).
    Requires SMTP_HOST env var — silently skips if not configured.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)

    import os
    import secrets
    user_row = await queries.get_user_by_email(db, body.email.strip())
    if user_row:
        token   = secrets.token_urlsafe(32)
        expiry  = int(_time.time()) + 3600  # 1 hour
        await queries.set_password_reset_token(db, user_row["id"], token, expiry)

        smtp_host = os.getenv("SMTP_HOST", "")
        if smtp_host:
            try:
                await _send_reset_email(body.email, user_row["username"], token, request)
            except Exception as exc:
                _log.error("Failed to send reset email to %s: %s", body.email, exc)

    # Always return success (never reveal whether email exists)
    return {"ok": True, "message": "If that email is registered, a reset link has been sent"}


@app.post("/api/auth/reset-password")
async def api_reset_password(request: Request, body: ResetPasswordRequest):
    """Validate reset token and update password."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    if len(body.new_password) < 8:
        return JSONResponse({"error": "Password must be at least 8 characters"}, status_code=400)
    if body.new_password != body.confirm_password:
        return JSONResponse({"error": "Passwords do not match"}, status_code=400)

    from auth.security import hash_password
    user_row = await queries.get_user_by_reset_token(db, body.token.strip())
    if not user_row:
        return JSONResponse({"error": "Invalid or expired reset link"}, status_code=400)
    if int(_time.time()) > user_row.get("password_reset_expiry", 0):
        return JSONResponse({"error": "Reset link has expired — request a new one"}, status_code=400)

    new_hash = hash_password(body.new_password)
    await queries.update_password(db, user_row["id"], new_hash)
    await queries.clear_password_reset_token(db, user_row["id"])
    return {"ok": True, "message": "Password reset successfully — you can now log in"}


async def _send_reset_email(email: str, username: str, token: str, request: Request) -> None:
    """Send a password-reset email via SMTP (configured via env vars)."""
    import os
    import smtplib
    from email.mime.text import MIMEText

    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    from_addr = os.getenv("SMTP_FROM", smtp_user)

    base_url  = str(request.base_url).rstrip("/")
    reset_url = f"{base_url}/?reset_token={token}"

    body = (
        f"Hi {username},\n\n"
        f"A password reset was requested for your CryptoDash account.\n\n"
        f"Click the link below (valid for 1 hour):\n{reset_url}\n\n"
        f"If you didn't request this, ignore this email."
    )
    msg = MIMEText(body)
    msg["Subject"] = "CryptoDash — Password Reset"
    msg["From"]    = from_addr
    msg["To"]      = email

    def _send():
        with smtplib.SMTP(smtp_host, smtp_port) as srv:
            srv.starttls()
            if smtp_user and smtp_pass:
                srv.login(smtp_user, smtp_pass)
            srv.sendmail(from_addr, [email], msg.as_string())

    await asyncio.to_thread(_send)


# ── Profile/Settings endpoints ──────────────────────────────────────────────────

@app.get("/api/profile/settings")
async def api_get_settings(request: Request, user: dict = Depends(_current_user)):
    """Return user settings — Telegram fields decrypted, CoinDCX keys masked."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    from auth.encryption import safe_decrypt
    user_id = int(user["sub"])
    row = await queries.get_user_settings(db, user_id)
    if not row:
        return {"telegram_token": None, "telegram_chat_id": None,
                "has_coindcx_key": False, "has_coindcx_secret": False}
    return {
        "telegram_token":     safe_decrypt(row.get("telegram_token")),
        "telegram_chat_id":   safe_decrypt(row.get("telegram_chat_id")),
        "has_coindcx_key":    bool(row.get("coindcx_api_key_enc")),
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

    # Encrypt Telegram credentials too (or keep existing encrypted values)
    tg_token_enc   = encrypt(body.telegram_token)   if body.telegram_token   else (existing or {}).get("telegram_token")
    tg_chat_id_enc = encrypt(body.telegram_chat_id) if body.telegram_chat_id else (existing or {}).get("telegram_chat_id")

    # Resolve plaintext for use in test message below
    tg_token   = body.telegram_token   or safe_decrypt((existing or {}).get("telegram_token"))
    tg_chat_id = body.telegram_chat_id or safe_decrypt((existing or {}).get("telegram_chat_id"))

    # Test CoinDCX connection if new keys provided
    if body.coindcx_api_key and body.coindcx_api_secret:
        from exchange.coindcx_client import CoinDCXClient
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

    # Save to DB (Telegram fields stored encrypted just like CoinDCX keys)
    await queries.upsert_user_settings(
        db, user_id,
        telegram_token=tg_token_enc,
        telegram_chat_id=tg_chat_id_enc,
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


# ── Admin endpoints ─────────────────────────────────────────────────────────────

async def _require_admin(request: Request, user: dict = Depends(_current_user)) -> dict:
    """Require admin. Fast-path checks JWT claim; falls back to DB for old tokens."""
    if user.get("is_admin"):
        return user
    # JWT issued before is_admin was added — verify directly from DB
    db = getattr(request.app.state, "db", None)
    if db:
        db_user = await queries.get_user_by_id(db, int(user["sub"]))
        if db_user and db_user.get("is_admin"):
            return user
    raise HTTPException(status_code=403, detail="Admin access required")


@app.get("/api/admin/users")
async def admin_list_users(request: Request, user: dict = Depends(_require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    return await queries.list_users(db)


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(
    request: Request, user_id: int, user: dict = Depends(_require_admin)
):
    if user_id == int(user["sub"]):
        return JSONResponse({"error": "Cannot delete your own account"}, status_code=400)
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    await queries.delete_user(db, user_id)
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/toggle-admin")
async def admin_toggle_admin(
    request: Request, user_id: int, user: dict = Depends(_require_admin)
):
    if user_id == int(user["sub"]):
        return JSONResponse({"error": "Cannot change your own admin status"}, status_code=400)
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    users = await queries.list_users(db)
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return JSONResponse({"error": "User not found"}, status_code=404)
    await queries.set_admin(db, user_id, not target["is_admin"])
    return {"ok": True, "is_admin": not target["is_admin"]}


@app.get("/api/admin/db-stats")
async def admin_db_stats(request: Request, user: dict = Depends(_require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    return await queries.db_stats(db)


@app.get("/api/admin/tables")
async def admin_list_tables(request: Request, user: dict = Depends(_require_admin)):
    """List all tables with their schemas."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    rows = await cursor.fetchall()
    tables = []
    for (name,) in rows:
        info_cur = await db.execute(f"PRAGMA table_info({name})")
        cols = await info_cur.fetchall()
        count_cur = await db.execute(f"SELECT COUNT(*) FROM {name}")
        count_row = await count_cur.fetchone()
        tables.append({
            "name": name,
            "row_count": count_row[0] if count_row else 0,
            "columns": [
                {"cid": c[0], "name": c[1], "type": c[2], "notnull": c[3],
                 "dflt_value": c[4], "pk": c[5]}
                for c in cols
            ],
        })
    return tables


@app.get("/api/admin/table/{table_name}")
async def admin_browse_table(
    request: Request,
    table_name: str,
    page:    int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    order_by: Optional[str] = Query(default=None),
    order_dir: str = Query(default="DESC"),
    filter_col: Optional[str] = Query(default=None),
    filter_val: Optional[str] = Query(default=None),
    user: dict = Depends(_require_admin),
):
    """Browse a table with pagination, sorting, and simple column filtering."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    # Validate table name (prevent injection via identifier)
    valid_cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    if not await valid_cur.fetchone():
        return JSONResponse({"error": "Table not found"}, status_code=404)
    # Validate column names for sorting/filtering
    info_cur = await db.execute(f"PRAGMA table_info({table_name})")
    col_rows = await info_cur.fetchall()
    valid_cols = {c[1] for c in col_rows}
    col_names  = [c[1] for c in col_rows]
    safe_order = order_by if order_by in valid_cols else (col_names[0] if col_names else "rowid")
    safe_dir   = "ASC" if order_dir.upper() == "ASC" else "DESC"
    where, params = "", []
    if filter_col and filter_col in valid_cols and filter_val is not None:
        where  = f"WHERE {filter_col} LIKE ?"
        params = [f"%{filter_val}%"]
    offset = (page - 1) * page_size
    count_cur = await db.execute(f"SELECT COUNT(*) FROM {table_name} {where}", params)
    total_row = await count_cur.fetchone()
    total = total_row[0] if total_row else 0
    data_cur = await db.execute(
        f"SELECT rowid, * FROM {table_name} {where}"
        f" ORDER BY {safe_order} {safe_dir} LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    rows = await data_cur.fetchall()
    return {
        "table":     table_name,
        "columns":   col_names,
        "rows":      [list(r) for r in rows],   # rows[i][0] = rowid
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     max(1, -(-total // page_size)),
    }


async def _validate_table(db, table_name: str):
    """Return (valid_cols set, col_names list) or raise 404."""
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Table not found")
    info = await db.execute(f"PRAGMA table_info({table_name})")
    rows = await info.fetchall()
    return {c[1] for c in rows}, [c[1] for c in rows]


class RowData(BaseModel):
    data: dict   # {col_name: value}


class DeleteRowsRequest(BaseModel):
    rowids: Optional[list] = None   # None or empty = delete ALL


@app.post("/api/admin/table/{table_name}/row")
async def admin_insert_row(
    request: Request,
    table_name: str,
    body: RowData,
    user: dict = Depends(_require_admin),
):
    """Insert a new row. Columns not provided are omitted (DB defaults apply)."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    valid_cols, _ = await _validate_table(db, table_name)
    safe_data = {k: v for k, v in body.data.items() if k in valid_cols}
    if not safe_data:
        return JSONResponse({"error": "No valid columns provided"}, status_code=400)
    cols   = ", ".join(safe_data.keys())
    placeholders = ", ".join("?" * len(safe_data))
    try:
        cur = await db.execute(
            f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})",
            list(safe_data.values()),
        )
        await db.commit()
        return {"ok": True, "rowid": cur.lastrowid}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.put("/api/admin/table/{table_name}/row/{rowid}")
async def admin_update_row(
    request: Request,
    table_name: str,
    rowid: int,
    body: RowData,
    user: dict = Depends(_require_admin),
):
    """Update a row by its SQLite rowid."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    valid_cols, _ = await _validate_table(db, table_name)
    safe_data = {k: v for k, v in body.data.items() if k in valid_cols}
    if not safe_data:
        return JSONResponse({"error": "No valid columns provided"}, status_code=400)
    set_clause = ", ".join(f"{k} = ?" for k in safe_data)
    try:
        await db.execute(
            f"UPDATE {table_name} SET {set_clause} WHERE rowid = ?",
            list(safe_data.values()) + [rowid],
        )
        await db.commit()
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete("/api/admin/table/{table_name}/row/{rowid}")
async def admin_delete_row(
    request: Request,
    table_name: str,
    rowid: int,
    user: dict = Depends(_require_admin),
):
    """Delete a single row by SQLite rowid."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    await _validate_table(db, table_name)
    await db.execute(f"DELETE FROM {table_name} WHERE rowid = ?", (rowid,))
    await db.commit()
    return {"ok": True}


@app.delete("/api/admin/table/{table_name}/rows")
async def admin_delete_rows(
    request: Request,
    table_name: str,
    body: DeleteRowsRequest,
    user: dict = Depends(_require_admin),
):
    """Delete selected rows (by rowid list) or ALL rows if rowids is null/empty."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    await _validate_table(db, table_name)
    if body.rowids:
        placeholders = ",".join("?" * len(body.rowids))
        cur = await db.execute(
            f"DELETE FROM {table_name} WHERE rowid IN ({placeholders})", body.rowids
        )
    else:
        cur = await db.execute(f"DELETE FROM {table_name}")
    await db.commit()
    return {"ok": True, "deleted": cur.rowcount}


class QueryRequest(BaseModel):
    sql: str


@app.post("/api/admin/query")
async def admin_run_query(
    request: Request,
    body: QueryRequest,
    user: dict = Depends(_require_admin),
):
    """Execute a read-only SQL query. Only SELECT statements allowed."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    sql = body.sql.strip()
    # Only allow SELECT queries
    if not sql.upper().startswith("SELECT"):
        return JSONResponse({"error": "Only SELECT queries are allowed"}, status_code=400)
    try:
        cursor = await db.execute(sql)
        rows = await cursor.fetchmany(1000)   # cap at 1000 rows
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        return {"columns": col_names, "rows": [list(r) for r in rows], "count": len(rows)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/admin/clear-signals")
async def admin_clear_signals(request: Request, user: dict = Depends(_require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    deleted = await queries.clear_signals(db)
    return {"ok": True, "deleted": deleted}


@app.post("/api/admin/clear-all")
async def admin_clear_all(request: Request, user: dict = Depends(_require_admin)):
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    counts = await queries.clear_all_data(db)
    return {"ok": True, "deleted": counts}


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/portfolio")
async def api_portfolio(request: Request, user: dict = Depends(_current_user)):
    """Proxy to CoinDCX balance endpoint using the user's own API keys."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    user_id = int(user["sub"])
    try:
        exchange, session = await _get_user_exchange(db, user_id, request)
        async with session:
            balances = await exchange.get_balances()
        return balances
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/signals/history")
async def api_signals_history(
    request:  Request,
    symbol:   Optional[str] = Query(default=None),
    interval: Optional[str] = Query(default=None),
    limit:    int            = Query(default=100),
    user:     dict           = Depends(_current_user),
):
    """Return recent signals for the user's watched (symbol, interval) pairs."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return []
    user_id = int(user["sub"])
    return await queries.get_recent_signals(
        db, symbol=symbol, interval=interval, limit=limit, user_id=user_id
    )


@app.delete("/api/signals/{signal_id}")
async def api_delete_signal(request: Request, signal_id: int, user: dict = Depends(_current_user)):
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
    user:       dict          = Depends(_current_user),
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
async def api_get_triggers(request: Request, user: dict = Depends(_current_user)):
    """List triggers belonging to the authenticated user, enriched with position data."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return []
    user_id  = int(user["sub"])
    triggers = await queries.get_triggers(db, active_only=False, user_id=user_id)
    # Attach current position data to each trigger
    for trig in triggers:
        pos = await queries.get_trigger_position(db, trig["id"])
        trig["coins_held"] = float(pos["coins_held"]) if pos else 0.0
        trig["avg_entry"]  = float(pos["avg_entry"])  if pos else 0.0
    return triggers


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
        exchange, session = await _get_user_exchange(db, user_id, request)
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

    leverage = max(1, min(int(body.leverage or 1), 20))
    tid = await queries.create_trigger(
        db, sym, iv, conf,
        adx_threshold=body.adx_threshold,
        cooldown_bars=body.cooldown_bars,
        name=body.name,
        trade_amount_usdt=body.trade_amount_usdt,
        user_id=user_id,
        market_type=body.market_type or "spot",
        leverage=leverage,
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
        market_type=body.market_type,
        leverage=body.leverage,
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


@app.delete("/api/triggers/{trigger_id}/position")
async def api_reset_trigger_position(
    request: Request,
    trigger_id: int,
    user: dict = Depends(_current_user),
):
    """
    Clear the DB position for a trigger (coins_held → 0).
    Use when the DB is out of sync with the exchange (e.g. coins sold manually).
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not ready"}, status_code=503)
    trig = await queries.get_trigger(db, trigger_id)
    if not trig or trig.get("user_id") != int(user["sub"]):
        raise HTTPException(status_code=404, detail="Trigger not found")
    await queries.upsert_trigger_position(db, trigger_id, trig["symbol"], 0.0, 0.0, 0.0)
    return {"ok": True, "message": f"Position cleared for trigger #{trigger_id}"}


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
    user:         dict  = Depends(_current_user),
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

    user_id = int(user["sub"])
    signals = await queries.get_signals_for_analytics(
        db,
        symbol     = None if symbol     == "ALL" else symbol,
        interval   = None if interval   == "ALL" else interval,
        confidence = None if confidence == "ALL" else confidence,
        since      = since,
        user_id    = user_id,
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


@app.get("/api/analytics/daily")
async def api_daily_pnl(
    request: Request,
    date:    str  = Query(default=""),
    user:    dict = Depends(_current_user),
):
    """
    Daily P&L summary grouped by trigger for a 10am IST → 10am IST window.
    If `date` (YYYY-MM-DD) is given, returns window ending at 10am IST on that date.
    Defaults to the most recently completed 10am-to-10am window.
    """
    import datetime as _dt
    from database.queries import _ist_10am_window

    db      = request.app.state.db
    user_id = int(user["sub"])

    if date:
        # Window ending at 10am IST on the given date
        IST_OFFSET = 19800
        end_ist    = _dt.datetime.strptime(date, "%Y-%m-%d").replace(hour=10, minute=0, second=0)
        to_ts      = int((end_ist - _dt.timedelta(seconds=IST_OFFSET) - _dt.datetime(1970, 1, 1)).total_seconds())
        from_ts    = to_ts - 86400
        start_ist  = end_ist - _dt.timedelta(days=1)
        label      = f"{start_ist.strftime('%b %d %H:%M')} → {end_ist.strftime('%b %d %H:%M')} IST"
    else:
        from_ts, to_ts, label = _ist_10am_window()

    rows = await queries.get_daily_pnl_by_trigger(db, user_id, from_ts, to_ts, label)
    return {"window": label, "rows": rows}


@app.post("/api/analytics/daily/send-report")
async def api_send_daily_pnl_report(
    request: Request,
    date:    str  = Query(default=""),
    user:    dict = Depends(_current_user),
):
    """
    Manually send the daily P&L Telegram report for the selected window to the
    current user. Uses the same 10am→10am IST window logic as the GET endpoint.
    """
    import datetime as _dt
    import traceback as _tb
    from database.queries import _ist_10am_window
    from telegram_bot.alerts import send_daily_pnl_report
    from auth.encryption import safe_decrypt
    from telegram import Bot

    db      = request.app.state.db
    user_id = int(user["sub"])

    if date:
        IST_OFFSET = 19800
        end_ist    = _dt.datetime.strptime(date, "%Y-%m-%d").replace(hour=10, minute=0, second=0)
        to_ts      = int((end_ist - _dt.timedelta(seconds=IST_OFFSET) - _dt.datetime(1970, 1, 1)).total_seconds())
        from_ts    = to_ts - 86400
        start_ist  = end_ist - _dt.timedelta(days=1)
        label      = f"{start_ist.strftime('%b %d %H:%M')} → {end_ist.strftime('%b %d %H:%M')} IST"
    else:
        from_ts, to_ts, label = _ist_10am_window()

    settings   = await queries.get_user_settings(db, user_id)
    tg_token   = safe_decrypt((settings or {}).get("telegram_token"))
    tg_chat_id = safe_decrypt((settings or {}).get("telegram_chat_id"))
    if not tg_token or not tg_chat_id:
        return JSONResponse(
            {"error": "Telegram not configured — add your bot token and chat ID in Account Settings."},
            status_code=400,
        )

    rows = await queries.get_daily_pnl_by_trigger(db, user_id, from_ts, to_ts, label)
    try:
        bot = Bot(token=tg_token)
        await send_daily_pnl_report(bot, tg_chat_id, rows, label)
        return {"ok": True, "window": label, "triggers": sum(1 for r in rows if r["trigger_id"] is not None)}
    except Exception as exc:
        _log.error("Manual P&L report send failed for user %d: %s\n%s", user_id, exc, _tb.format_exc())
        return JSONResponse({"error": f"Telegram send failed: {exc}"}, status_code=500)


@app.get("/api/analytics/real-trades")
async def api_real_trades_analytics(
    request: Request,
    period:  str  = Query(default="7d"),
    user:    dict = Depends(_current_user),
):
    """
    Real trade P&L based on actual executed orders stored in the DB.
    Groups orders into BUY→SELL cycles per (symbol, trigger_id).
    P&L is shown in USDT (actual money), not signal-price percentages.
    Only cycles with at least one BUY and one SELL are shown as completed.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)

    user_id = int(user["sub"])
    now     = int(_time.time())
    since   = now - _PERIOD_SECS[period] if period in _PERIOD_SECS else None

    orders = await queries.get_real_trades_for_user(db, user_id, since=since)
    if not orders:
        return {
            "total_cycles":        0,
            "completed_cycles":    0,
            "total_invested_usdt": 0.0,
            "total_returned_usdt": 0.0,
            "net_pnl_usdt":        0.0,
            "net_pnl_pct":         None,
            "win_rate":            None,
            "cycles":              [],
            "open_positions":      [],
        }

    # Group orders by (symbol, trigger_id) — each group is one trading position
    from collections import defaultdict
    groups: dict[tuple, list] = defaultdict(list)
    for o in orders:
        key = (o["symbol"], o["trigger_id"])
        groups[key].append(o)

    completed_cycles: list[dict] = []
    open_positions:   list[dict] = []

    FEE = 0.001  # CoinDCX taker fee per side (0.1%)

    for (symbol, trigger_id), group in groups.items():
        buys  = [o for o in group if o["side"] == "buy"]
        sells = [o for o in group if o["side"] == "sell"]

        # Gross order amounts (qty * signal_price, no fee)
        total_bought_qty  = sum(o["quantity"]    for o in buys)
        total_sold_qty    = sum(o["quantity"]    for o in sells)
        remaining_qty     = round(total_bought_qty - total_sold_qty, 8)

        # Fee-adjusted amounts:
        #   BUY  effective cost    = gross * (1 + FEE)  [we paid extra 0.1%]
        #   SELL effective proceeds = gross * (1 - FEE)  [we received 0.1% less]
        total_bought_gross = sum(o["usdt_amount"] for o in buys)
        total_sold_gross   = sum(o["usdt_amount"] for o in sells)
        total_bought_usdt  = total_bought_gross * (1 + FEE)   # actual cost incl. fee
        total_sold_usdt    = total_sold_gross   * (1 - FEE)   # actual proceeds after fee

        # Gross prices (signal prices) for display reference
        avg_buy_price_gross  = total_bought_gross / total_bought_qty if total_bought_qty else 0
        avg_sell_price_gross = total_sold_gross   / total_sold_qty   if total_sold_qty   else 0
        # Effective (fee-adjusted) prices per coin
        avg_buy_price_eff    = avg_buy_price_gross  * (1 + FEE)   # true cost per coin
        avg_sell_price_eff   = avg_sell_price_gross * (1 - FEE)   # true proceeds per coin
        # Break-even sell price: what sell price makes P&L exactly 0
        breakeven_price      = avg_buy_price_eff / (1 - FEE) if avg_buy_price_eff else 0

        if sells:
            # Proportion of buys that were sold (handles partial exits)
            sell_fraction = min(total_sold_qty / total_bought_qty, 1.0) if total_bought_qty else 0
            cost_for_sold = round(total_bought_usdt * sell_fraction, 4)
            net_pnl_usdt  = round(total_sold_usdt - cost_for_sold, 4)
            net_pnl_pct   = round(net_pnl_usdt / cost_for_sold * 100, 4) if cost_for_sold else 0.0
            fee_cost_usdt = round(
                total_bought_gross * FEE * sell_fraction + total_sold_gross * FEE, 4
            )

            completed_cycles.append({
                "symbol":              symbol,
                "trigger_id":         trigger_id,
                "buy_count":          len(buys),
                "sell_count":         len(sells),
                # Gross (signal price × qty) — what you see on the order form
                "total_bought_gross": round(total_bought_gross, 4),
                "total_sold_gross":   round(total_sold_gross, 4),
                # Fee-adjusted — actual money in/out of your account
                "total_bought_usdt":  round(total_bought_usdt, 4),
                "total_sold_usdt":    round(total_sold_usdt, 4),
                "fee_cost_usdt":      fee_cost_usdt,
                # Prices
                "avg_buy_price":      round(avg_buy_price_gross, 4),
                "avg_buy_eff":        round(avg_buy_price_eff, 4),   # true cost/coin with fee
                "avg_sell_price":     round(avg_sell_price_gross, 4),
                "avg_sell_eff":       round(avg_sell_price_eff, 4),  # true proceeds/coin after fee
                "breakeven_price":    round(breakeven_price, 4),
                # P&L (fee-adjusted)
                "net_pnl_usdt":       net_pnl_usdt,
                "net_pnl_pct":        net_pnl_pct,
                "remaining_qty":      max(remaining_qty, 0.0),
                "is_closed":          remaining_qty < 1e-8,
                "first_buy_time":     buys[0]["created_at"]  if buys  else None,
                "last_sell_time":     sells[-1]["created_at"] if sells else None,
                "orders":             [
                    {
                        "side":       o["side"],
                        "qty":        round(o["quantity"], 8),
                        "price":      round(o["price"], 4),
                        "usdt":       round(o["usdt_amount"], 4),
                        "time":       o["created_at"],
                        "pnl_pct":    o.get("pnl_pct"),
                    }
                    for o in sorted(group, key=lambda x: x["created_at"])
                ],
            })
        elif buys:
            # Only buys so far — open position
            open_positions.append({
                "symbol":            symbol,
                "trigger_id":        trigger_id,
                "buy_count":         len(buys),
                "total_bought_usdt": round(total_bought_usdt, 4),
                "qty_held":          round(total_bought_qty, 8),
                "avg_buy_price":     round(avg_buy_price_gross, 4),
                "avg_buy_eff":       round(avg_buy_price_eff, 4),
                "breakeven_price":   round(breakeven_price, 4),
                "since":             buys[0]["created_at"],
            })

    completed_cycles.sort(key=lambda c: c["last_sell_time"] or 0, reverse=True)

    total_invested = sum(c["total_bought_usdt"] for c in completed_cycles)
    total_returned = sum(c["total_sold_usdt"]   for c in completed_cycles)
    net_pnl_usdt   = round(sum(c["net_pnl_usdt"]  for c in completed_cycles), 4)
    net_pnl_pct    = round(net_pnl_usdt / total_invested * 100, 4) if total_invested else None
    wins           = [c for c in completed_cycles if c["net_pnl_usdt"] > 0]
    win_rate       = round(len(wins) / len(completed_cycles) * 100, 1) if completed_cycles else None
    total_fees     = round(sum(c["fee_cost_usdt"] for c in completed_cycles), 4)

    return {
        "total_cycles":        len(completed_cycles) + len(open_positions),
        "completed_cycles":    len(completed_cycles),
        "total_invested_usdt": round(total_invested, 4),
        "total_returned_usdt": round(total_returned, 4),
        "total_fees_usdt":     total_fees,
        "net_pnl_usdt":        net_pnl_usdt,
        "net_pnl_pct":         net_pnl_pct,
        "win_rate":            win_rate,
        "cycles":              completed_cycles[:50],
        "open_positions":      open_positions,
    }


@app.get("/api/trades/coindcx")
async def api_coindcx_trades(
    request: Request,
    limit:   int  = Query(default=200, ge=1, le=500),
    user:    dict = Depends(_current_user),
):
    """
    Fetch real trade fills from CoinDCX (using the logged-in user's API keys).
    Returns per-trade detail (USDT + INR), aggregate totals, and current balances.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)

    user_id = int(user["sub"])
    exchange, session = await _get_user_exchange(db, user_id, request)

    inr_rate: float = getattr(request.app.state, "inr_rate", None) or 83.5

    async with session:
        try:
            fills = await exchange.get_trade_history(limit=limit)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"CoinDCX trade history failed: {exc}")
        try:
            balances = await exchange.get_balances()
        except Exception:
            balances = []

    trades_out = []
    total_bought_usdt  = 0.0
    total_bought_inr   = 0.0
    total_sold_usdt    = 0.0
    total_sold_inr     = 0.0
    total_fee_usdt     = 0.0
    total_fee_inr      = 0.0

    for f in fills:
        try:
            side      = (f.get("side") or "").upper()
            market    = f.get("market") or ""
            price     = float(f.get("price") or 0)
            quantity  = float(f.get("quantity") or 0)
            fee_amt   = float(f.get("fee_amount") or 0)
            fee_cur   = (f.get("fee_currency") or "USDT").upper()
            ts_ms     = int(f.get("timestamp") or 0)

            # Gross trade value in USDT
            gross_usdt = price * quantity
            gross_inr  = gross_usdt * inr_rate

            # Normalise fee to USDT equivalent (if fee is in base asset, convert via price)
            if fee_cur == "USDT" or fee_cur.endswith("USDT"):
                fee_usdt = fee_amt
            else:
                fee_usdt = fee_amt * price   # base asset fee → USDT

            fee_inr = fee_usdt * inr_rate

            # Net cost (BUY) or net revenue (SELL) in USDT
            if side == "BUY":
                net_usdt = gross_usdt + fee_usdt
                net_inr  = net_usdt * inr_rate
                total_bought_usdt += net_usdt
                total_bought_inr  += net_inr
            else:
                net_usdt = gross_usdt - fee_usdt
                net_inr  = net_usdt * inr_rate
                total_sold_usdt += net_usdt
                total_sold_inr  += net_inr

            total_fee_usdt += fee_usdt
            total_fee_inr  += fee_inr

            trades_out.append({
                "market":      market,
                "side":        side,
                "price_usdt":  round(price, 6),
                "price_inr":   round(price * inr_rate, 4),
                "quantity":    round(quantity, 8),
                "gross_usdt":  round(gross_usdt, 4),
                "gross_inr":   round(gross_inr, 2),
                "fee_usdt":    round(fee_usdt, 6),
                "fee_inr":     round(fee_inr, 4),
                "net_usdt":    round(net_usdt, 4),    # cost if BUY, revenue if SELL
                "net_inr":     round(net_inr, 2),
                "fee_currency": fee_cur,
                "timestamp":   ts_ms // 1000 if ts_ms > 1e10 else ts_ms,
            })
        except Exception:
            continue

    # Most recent first
    trades_out.sort(key=lambda t: t["timestamp"], reverse=True)

    net_pnl_usdt = total_sold_usdt - total_bought_usdt
    net_pnl_inr  = total_sold_inr  - total_bought_inr

    return {
        "inr_rate":          round(inr_rate, 4),
        "trades":            trades_out,
        "total_trades":      len(trades_out),
        "summary": {
            "total_bought_usdt": round(total_bought_usdt, 4),
            "total_bought_inr":  round(total_bought_inr, 2),
            "total_sold_usdt":   round(total_sold_usdt, 4),
            "total_sold_inr":    round(total_sold_inr, 2),
            "total_fee_usdt":    round(total_fee_usdt, 4),
            "total_fee_inr":     round(total_fee_inr, 2),
            "net_pnl_usdt":      round(net_pnl_usdt, 4),
            "net_pnl_inr":       round(net_pnl_inr, 2),
        },
        "balances": balances,
    }


@app.get("/api/adaptive")
async def api_adaptive_state(request: Request, user: dict = Depends(_current_user)):
    """Return current adaptive engine state and position-size recommendations."""
    from signals.adaptive_strategy import engine as _adaptive_engine
    return _adaptive_engine.summary()


# ── Futures API ────────────────────────────────────────────────────────────────

@app.get("/api/futures/positions")
async def api_futures_positions(
    request: Request,
    user: dict = Depends(_current_user),
):
    """All open futures positions for the authenticated user."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    user_id   = int(user["sub"])
    positions = await queries.get_all_open_futures_positions(db, user_id=user_id)
    return {"positions": positions}


@app.get("/api/futures/history")
async def api_futures_history(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(_current_user),
):
    """Closed futures position history for the authenticated user."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    user_id = int(user["sub"])
    history = await queries.get_futures_history(db, user_id, limit=limit)
    return {"history": history}


@app.post("/api/futures/positions/{position_id}/close")
async def api_close_futures_position(
    request: Request,
    position_id: int,
    user: dict = Depends(_current_user),
):
    """Manually close an open futures position at current market price."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    user_id = int(user["sub"])

    cursor = await db.execute(
        "SELECT * FROM futures_positions WHERE id = ? AND user_id = ? AND status = 'open'",
        (position_id, user_id),
    )
    row = await cursor.fetchone()
    if not row:
        return JSONResponse({"error": "Position not found or already closed"}, status_code=404)
    fpos = dict(row)

    try:
        exchange, session = await _get_user_exchange(db, user_id, request)
        async with session:
            close_side = "sell" if fpos["side"] == "long" else "buy"
            result = await exchange.close_futures_position(
                side=close_side,
                pair=fpos["symbol"],
                quantity=fpos["quantity"],
                leverage=fpos["leverage"],
            )
        close_order_id = str(result.get("id") or result.get("order_id") or "")
        # Use entry price as approximate close price (real fill unknown synchronously)
        close_price = fpos["entry_price"]
        await queries.close_futures_position(
            db, position_id,
            close_price=close_price,
            pnl_pct=0.0,
            pnl_usdt=0.0,
            cdx_close_order_id=close_order_id,
        )
        return {"ok": True, "order_id": close_order_id}
    except Exception:
        _log.exception("Failed to close futures position", extra={"position_id": position_id, "user_id": user_id})
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.get("/api/currency/inr-rate")
async def api_inr_rate(request: Request):
    """Return cached USD→INR exchange rate. Refreshes from frankfurter.app every hour."""
    cached = getattr(request.app.state, "inr_rate", None)
    cached_at = getattr(request.app.state, "inr_rate_ts", 0)
    now = int(_time.time())
    # Serve cache if younger than 1 hour
    if cached and (now - cached_at) < 3600:
        return {"usd_to_inr": cached, "cached_at": cached_at, "source": "cache"}
    # Fetch fresh rate
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                "https://api.frankfurter.app/latest?from=USD&to=INR",
                timeout=aiohttp.ClientTimeout(total=8),
            )
            data = await resp.json()
            rate = data["rates"]["INR"]
            request.app.state.inr_rate    = rate
            request.app.state.inr_rate_ts = now
            return {"usd_to_inr": rate, "cached_at": now, "source": "live"}
    except Exception as exc:
        _log.warning("INR rate fetch failed: %s", exc)
        fallback = cached or 83.5   # reasonable fallback
        return {"usd_to_inr": fallback, "cached_at": cached_at, "source": "fallback"}


@app.on_event("startup")
async def _on_startup():
    """
    Auto-init DB if not already injected by orchestrator (covers run_ui.py standalone mode).
    Then restore adaptive engine state and start the background monitor.
    """
    import asyncio
    import os
    # Record when this server process started — used to invalidate pre-restart tokens
    app.state.startup_time = int(_time.time())
    # Standalone mode: no orchestrator set app.state.db — initialise it now
    if getattr(app.state, "db", None) is None:
        from database.db import init_db
        db_path = os.environ.get("DB_PATH", "data/trading.db")
        app.state.db = await init_db(db_path)
    asyncio.create_task(_load_adaptive_state_when_ready())
    asyncio.create_task(_inr_rate_refresh_loop())
    asyncio.create_task(_daily_pnl_scheduler())


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
            # Only restore state if there are real trades to back it up.
            # If no real trades exist, don't restore stale state — it causes
            # phantom losses that block legitimate BUYs.
            real_trades = await queries.get_real_completed_pnls(db)
            if real_trades:
                saved = await queries.load_adaptive_state(db)
                if saved:
                    _adaptive_engine.from_dict(saved)
                    logging.getLogger("adaptive_monitor").info(
                        "Adaptive engine restored: %d real trades  WR=%.0f%%  Kelly=%.1f%%",
                        len(real_trades),
                        _adaptive_engine.win_rate * 100,
                        _adaptive_engine.kelly_fraction() * 100,
                    )
            else:
                logging.getLogger("adaptive_monitor").info(
                    "Adaptive engine: no real trades yet — starting fresh (conservative defaults)"
                )
        except Exception:
            pass
        asyncio.create_task(_adaptive_monitor_loop())
        return


async def _inr_rate_refresh_loop() -> None:
    """Fetch USD→INR rate on startup and refresh every hour."""
    import aiohttp
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    "https://api.frankfurter.app/latest?from=USD&to=INR",
                    timeout=aiohttp.ClientTimeout(total=8),
                )
                data = await resp.json()
                app.state.inr_rate    = data["rates"]["INR"]
                app.state.inr_rate_ts = int(_time.time())
        except Exception as exc:
            _log.warning("INR rate refresh failed: %s", exc)
            if not getattr(app.state, "inr_rate", None):
                app.state.inr_rate    = 83.5   # fallback
                app.state.inr_rate_ts = int(_time.time())
        await asyncio.sleep(3600)   # refresh every hour



async def _send_pnl_report_to_all_users(db, from_ts: int, to_ts: int, window_label: str, log) -> None:
    """Send the daily P&L report for the given 10am→10am IST window to every user with Telegram configured."""
    import traceback as _tb
    from telegram_bot.alerts import send_daily_pnl_report
    from auth.encryption import safe_decrypt
    from telegram import Bot

    users = await queries.get_all_users(db)
    if not users:
        log.info("Daily P&L: no users found")
        return

    for user in users:
        user_id = user["id"]
        try:
            settings   = await queries.get_user_settings(db, user_id)
            tg_token   = safe_decrypt((settings or {}).get("telegram_token"))
            tg_chat_id = safe_decrypt((settings or {}).get("telegram_chat_id"))
            if not tg_token or not tg_chat_id:
                log.debug("Daily P&L: user %d has no Telegram configured — skipping", user_id)
                continue

            rows = await queries.get_daily_pnl_by_trigger(db, user_id, from_ts, to_ts, window_label)
            bot  = Bot(token=tg_token)

            # Retry once on Telegram error (transient network issues)
            for attempt in (1, 2):
                try:
                    await send_daily_pnl_report(bot, tg_chat_id, rows, window_label)
                    log.info(
                        "Daily P&L report sent to user %d for %s  "
                        "(%d trigger rows, net P&L %.4f USDT)",
                        user_id, window_label,
                        sum(1 for r in rows if r["trigger_id"] is not None),
                        (rows[-1]["net_pnl_usdt"] if rows else 0.0),
                    )
                    break
                except Exception as tg_exc:
                    if attempt == 1:
                        log.warning(
                            "Daily P&L: Telegram send failed for user %d (attempt 1), retrying in 30s: %s",
                            user_id, tg_exc,
                        )
                        import asyncio as _aio
                        await _aio.sleep(30)
                    else:
                        log.error(
                            "Daily P&L: Telegram send failed for user %d after retry: %s\n%s",
                            user_id, tg_exc, _tb.format_exc(),
                        )

        except Exception as exc:
            log.error(
                "Daily P&L: error building report for user %d: %s\n%s",
                user_id, exc, _tb.format_exc(),
            )


async def _daily_pnl_scheduler() -> None:
    """
    Fire a daily P&L report to every active user's Telegram at 10:00 AM IST (04:30 UTC).

    Startup catch-up: if the service starts after 04:30 UTC today and today's report
    hasn't been sent yet, it fires immediately for today's date, then schedules normally.
    """
    import asyncio
    import datetime as _dt
    import logging

    log = logging.getLogger("daily_pnl")

    # Brief startup delay so DB is ready
    await asyncio.sleep(5)

    REPORT_HOUR_UTC   = 4
    REPORT_MINUTE_UTC = 30

    # ── Startup catch-up ──────────────────────────────────────────────────────
    # If today's 04:30 UTC has already passed, send now for the just-completed window.
    now_utc      = _dt.datetime.utcnow()
    today_target = now_utc.replace(
        hour=REPORT_HOUR_UTC, minute=REPORT_MINUTE_UTC, second=0, microsecond=0
    )
    last_sent_label: str = ""   # tracks which window was last reported

    if now_utc > today_target:
        from database.queries import _ist_10am_window
        from_ts, to_ts, window_label = _ist_10am_window(now_utc)
        db = getattr(app.state, "db", None)
        if db is not None:
            log.info("Daily P&L: service started after 04:30 UTC — sending catch-up report for %s", window_label)
            try:
                await _send_pnl_report_to_all_users(db, from_ts, to_ts, window_label, log)
                last_sent_label = window_label
            except Exception as exc:
                import traceback as _tb
                log.error("Daily P&L catch-up failed: %s\n%s", exc, _tb.format_exc())

    # ── Normal nightly loop ───────────────────────────────────────────────────
    while True:
        now_utc = _dt.datetime.utcnow()
        target  = now_utc.replace(
            hour=REPORT_HOUR_UTC, minute=REPORT_MINUTE_UTC, second=0, microsecond=0
        )
        if now_utc >= target:
            target += _dt.timedelta(days=1)
        wait_sec = (target - now_utc).total_seconds()
        log.info(
            "Daily P&L: next report in %.0f s  (at %s UTC / 10:00 IST)",
            wait_sec, target.strftime("%Y-%m-%d %H:%M"),
        )
        await asyncio.sleep(wait_sec)

        db = getattr(app.state, "db", None)
        if db is None:
            log.warning("Daily P&L: DB not available at report time — skipping")
            continue

        from database.queries import _ist_10am_window
        from_ts, to_ts, window_label = _ist_10am_window(_dt.datetime.utcnow())

        # Guard against double-send on very fast restarts
        if window_label == last_sent_label:
            log.info("Daily P&L: report for %s already sent — skipping duplicate", window_label)
            continue

        try:
            await _send_pnl_report_to_all_users(db, from_ts, to_ts, window_label, log)
            last_sent_label = window_label
        except Exception as exc:
            import traceback as _tb
            log.error("Daily P&L scheduler error: %s\n%s", exc, _tb.format_exc())


async def _adaptive_monitor_loop() -> None:
    """
    Background task: every 5 min, read REAL executed trades from trade_history,
    update the adaptive engine, persist to DB.

    Uses only actual CoinDCX SELL fills (with recorded P&L) — never simulated
    signal pairings.  If no real trades exist yet, the engine keeps conservative
    defaults without tripping any circuit breaker.
    """
    import asyncio
    import logging
    from signals.adaptive_strategy import engine as _adaptive_engine
    log = logging.getLogger("adaptive_monitor")

    while True:
        await asyncio.sleep(300)   # 5-minute cycle
        db = getattr(app.state, "db", None)
        if db is None:
            continue
        try:
            real_trades = await queries.get_real_completed_pnls(db)

            if not real_trades:
                # No real trades yet — log state but don't update engine
                # (keeps conservative defaults, no artificial losses)
                log.info(
                    "Adaptive engine: 0 real trades yet — holding defaults  "
                    "Kelly=%.1f%%  DD=0.0%%",
                    _adaptive_engine.kelly_fraction() * 100,
                )
                continue

            # Enrich with running portfolio value (starts at 100 for relative calc)
            running = 100.0
            enriched = []
            for t in real_trades:
                running = running * (1 + t["pnl_pct"] / 100)
                enriched.append({"pnl_pct": t["pnl_pct"], "portfolio_val": running})

            from exchange.auto_trade import _engine_lock
            async with _engine_lock:
                _adaptive_engine.update(enriched)
            await queries.save_adaptive_state(db, _adaptive_engine.to_dict())

            losses = _adaptive_engine.consecutive_losses
            dd     = _adaptive_engine.current_drawdown_pct
            # Describe what the adaptive filter will enforce next trade
            if losses >= 5:
                adapt_note = "STRICT (≥5 losses) — HIGH + ADX≥30 required"
            elif losses >= 3:
                adapt_note = "SELECTIVE (≥3 losses) — HIGH confidence required"
            else:
                adapt_note = "NORMAL"
            log.info(
                "Adaptive engine: %d real trades  WR=%.0f%%  Kelly=%.1f%%  DD=%.1f%%  "
                "losses=%d  filter=%s",
                len(real_trades),
                _adaptive_engine.win_rate * 100,
                _adaptive_engine.kelly_fraction() * 100,
                dd, losses, adapt_note,
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

                    # Check which active triggers match this signal (user-owned only)
                    matched_triggers: list[dict] = []
                    if db is not None:
                        try:
                            active_triggers = await queries.get_triggers(db)
                            matched_triggers = [
                                t for t in active_triggers
                                if t.get("user_id") is not None
                                and queries.trigger_matches(
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
                    # Auto-trading is handled by the background worker (orchestrator.py),
                    # which runs 24/7 regardless of browser state.
                    # The WebSocket only streams signal data to the UI.
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
