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
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from signals.detector import SignalDetector
from streaming.stream import stream_klines, fetch_historical_klines
from database import queries

SEED_BARS  = 201
static_dir = Path(__file__).parent / "static"

app = FastAPI()
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


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
    symbol:   str = Query(...),
    interval: str = Query(default="1m"),
    limit:    int = Query(default=50),
):
    """Return recent signals from SQLite for pre-populating the dashboard sidebar."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return []
    return await queries.get_recent_signals(db, symbol=symbol, interval=interval, limit=limit)


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    symbol:    str = Query(default="ethusdt"),
    interval:  str = Query(default="1m"),
):
    await websocket.accept()
    task = asyncio.create_task(
        _connection_loop(websocket, symbol.lower(), interval, websocket.app)
    )
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _connection_loop(ws: WebSocket, symbol: str, interval: str, app_state) -> None:
    """Fetch history, send it, then stream live candles + signals to one client."""
    db       = getattr(app_state.state, "db",           None)
    tg_bot   = getattr(app_state.state, "telegram_bot", None)
    settings = getattr(app_state.state, "settings",     None)

    detector = SignalDetector()
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
                    signal_payload = {
                        "type":        "signal",
                        "direction":   signal.direction,
                        "confidence":  signal.confidence,
                        "entry_price": signal.entry_price,
                        "time":        signal.open_time // 1000,
                        "macd_val":    signal.macd_val,
                        "signal_val":  signal.signal_val,
                        "histogram":   signal.histogram,
                        "adx_val":     signal.adx_val,
                        "reasons":     signal.reasons,
                        "trend_note":  signal.trend_note,
                    }
                    await ws.send_json(signal_payload)

                    # Persist signal to DB
                    signal_id: Optional[int] = None
                    if db is not None:
                        try:
                            signal_id = await queries.insert_signal(db, symbol, interval, signal)
                        except Exception:
                            pass

                    # Send Telegram alert
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
