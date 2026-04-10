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
import time as _time
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from signals.detector import SignalDetector, params_for_interval
from streaming.stream import stream_klines, fetch_historical_klines
from database import queries

SEED_BARS  = 201
static_dir = Path(__file__).parent / "static"

# ── Trigger request models ──────────────────────────────────────────────────────

class TriggerCreate(BaseModel):
    symbol:         str
    interval:       str
    min_confidence: str            = "MEDIUM"
    adx_threshold:  Optional[float] = None   # None = use interval-tier default
    cooldown_bars:  Optional[int]   = None   # None = use interval-tier default

class TriggerBulkDelete(BaseModel):
    ids: list[int]

class TriggerUpdate(BaseModel):
    symbol:         Optional[str]   = None
    interval:       Optional[str]   = None
    min_confidence: Optional[str]   = None
    active:         Optional[bool]  = None
    adx_threshold:  Optional[float] = None
    cooldown_bars:  Optional[int]   = None

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
async def api_create_trigger(request: Request, body: TriggerCreate):
    """Create a new trigger and send a Telegram alert."""
    db       = getattr(request.app.state, "db",           None)
    tg_bot   = getattr(request.app.state, "telegram_bot", None)
    settings = getattr(request.app.state, "settings",     None)
    if db is None:
        return JSONResponse({"error": "DB not configured"}, status_code=503)
    sym  = body.symbol.upper()
    iv   = body.interval
    conf = body.min_confidence.upper()
    tid  = await queries.create_trigger(
        db, sym, iv, conf,
        adx_threshold=body.adx_threshold,
        cooldown_bars=body.cooldown_bars,
    )
    if tg_bot and settings:
        from telegram_bot.alerts import send_trigger_alert
        await send_trigger_alert(tg_bot, settings.telegram_chat_id, "created", sym, iv, conf)
    return {
        "id": tid, "symbol": sym, "interval": iv,
        "min_confidence": conf, "active": True,
        "adx_threshold": body.adx_threshold,
        "cooldown_bars": body.cooldown_bars,
    }


@app.put("/api/triggers/{trigger_id}")
async def api_update_trigger(request: Request, trigger_id: int, body: TriggerUpdate):
    """Update an existing trigger and send a Telegram alert."""
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
    buy_pct: float,
    sell_pct: float,
) -> dict:
    """
    Adaptive DCA portfolio simulation.

    Each (symbol, interval) stream is treated as an independent position.

    BUY rules:
      - Entry 1 (flat):  spend buy_pct% of total USDT
      - Entry 2 (DCA):   spend buy_pct/2% (half size to average down)
      - Entry 3 (DCA):   spend buy_pct/4% (quarter size)
      - Entry 4+: skipped — max 3 DCA entries per stream

    SELL rules (confidence-weighted partial exits):
      - HIGH:   close 100% of this stream's position
      - MEDIUM: close 60%
      - LOW:    close 30%
      Consecutive SELLs keep reducing until position is flat, then skip.

    Holdings and P&L are tracked per stream (symbol+interval).
    USDT pool is shared across all streams.
    """
    _MAX_DCA      = 3
    _DCA_SCALE    = [1.0, 0.5, 0.25]        # fraction of buy_pct for each DCA entry
    _SELL_RATIO   = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}

    usdt: float             = initial_usdt
    # Per-stream state
    stream_coins:   dict[tuple, float] = {}  # (sym, iv) → coins held
    stream_entries: dict[tuple, int]   = {}  # (sym, iv) → DCA entry count
    stream_avg:     dict[tuple, float] = {}  # (sym, iv) → avg entry price
    # Per-symbol last price (shared across intervals for valuation)
    last_price: dict[str, float] = {}
    # Global coin holdings (base → total coins, across all intervals)
    holdings: dict[str, float] = {}

    sim_trades:   list[dict] = []
    skipped_buys: int = 0
    skipped_sells: int = 0

    for sig in signals:   # oldest-first
        sym      = sig["symbol"]
        interval = sig["interval"]
        price    = sig["entry_price"]
        conf     = sig["confidence"]
        stream   = (sym, interval)
        last_price[sym] = price

        # Derive base currency
        base = sym
        for quote in ("USDT", "BTC", "ETH", "BNB", "INR"):
            if sym.endswith(quote):
                base = sym[: -len(quote)]
                break

        entries     = stream_entries.get(stream, 0)
        coins_held  = stream_coins.get(stream, 0.0)

        if sig["direction"] == "BUY":
            if entries >= _MAX_DCA:
                skipped_buys += 1
                continue                        # position at max — skip
            scale   = _DCA_SCALE[entries]
            spend   = usdt * (buy_pct / 100.0) * scale
            if spend < 0.0001 or usdt < spend:
                skipped_buys += 1
                continue
            coins   = spend / price
            usdt   -= spend
            # Update stream & global state
            stream_entries[stream] = entries + 1
            stream_coins[stream]   = coins_held + coins
            holdings[base]         = holdings.get(base, 0.0) + coins
            # Update average entry price (volume-weighted)
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
                "dca_entry":     entries + 1,
                "price":         round(price, 4),
                "usdt_spent":    round(spend, 4),
                "coins_bought":  round(coins, 8),
                "usdt_balance":  round(usdt, 4),
                "portfolio_val": round(portfolio_val, 4),
            })

        elif sig["direction"] == "SELL":
            if coins_held < 1e-10:
                skipped_sells += 1
                continue                        # already flat — skip

            ratio      = _SELL_RATIO.get(conf, 0.6)
            coins_sold = coins_held * ratio
            received   = coins_sold * price
            avg_entry  = stream_avg.get(stream, price)
            pnl_pct    = (price - avg_entry) / avg_entry * 100 if avg_entry else 0.0

            usdt   += received
            stream_coins[stream] = coins_held - coins_sold
            holdings[base]       = max(0.0, holdings.get(base, 0.0) - coins_sold)

            # Update entry count proportionally (or zero on full exit)
            if ratio >= 1.0 or stream_coins[stream] < 1e-10:
                stream_entries[stream] = 0
                stream_avg[stream]     = 0.0
                stream_coins[stream]   = 0.0
            else:
                new_count = max(0, round(entries * (1.0 - ratio)))
                stream_entries[stream] = new_count

            portfolio_val = usdt + sum(
                holdings.get(b, 0) * last_price.get(b + "USDT", last_price.get(sym, price))
                for b in holdings
            )
            sim_trades.append({
                "time":           sig["open_time"] // 1000,
                "symbol":         sym,
                "interval":       interval,
                "direction":      "SELL",
                "confidence":     conf,
                "exit_pct":       round(ratio * 100),
                "price":          round(price, 4),
                "usdt_received":  round(received, 4),
                "coins_sold":     round(coins_sold, 8),
                "pnl_pct":        round(pnl_pct, 3),
                "usdt_balance":   round(usdt, 4),
                "portfolio_val":  round(portfolio_val, 4),
            })

    # Final valuation
    holding_value = sum(
        holdings.get(b, 0) * last_price.get(b + "USDT", 0) for b in holdings
    )
    final_value  = usdt + holding_value
    total_return = (final_value - initial_usdt) / initial_usdt * 100

    return {
        "initial_usdt":       round(initial_usdt, 2),
        "buy_pct":            buy_pct,
        "sell_pct":           sell_pct,
        "final_cash_usdt":    round(usdt, 4),
        "final_holding_usdt": round(holding_value, 4),
        "final_value_usdt":   round(final_value, 4),
        "total_return_pct":   round(total_return, 3),
        "holdings":           {k: round(v, 8) for k, v in holdings.items() if v > 1e-10},
        "skipped_buys":       skipped_buys,
        "skipped_sells":      skipped_sells,
        "sim_trades":         sim_trades[-100:],
        "sim_trade_count":    len(sim_trades),
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
                "pnl_pct":     round(pnl_pct, 3),
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
            "total_pnl": round(sum(t["pnl_pct"] for t in subset), 3),
            "avg_pnl":   round(sum(t["pnl_pct"] for t in subset) / len(subset), 3),
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
        "avg_gain_pct":   round(sum(t["pnl_pct"] for t in wins)   / len(wins)   if wins   else 0, 3),
        "avg_loss_pct":   round(sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0, 3),
        "total_pnl_pct":  round(sum(t["pnl_pct"] for t in all_trades), 3),
        "best_trade_pct": round(max((t["pnl_pct"] for t in all_trades), default=0), 3),
        "worst_trade_pct":round(min((t["pnl_pct"] for t in all_trades), default=0), 3),
        "by_confidence":  by_confidence,
        "by_symbol":      by_symbol,
        "trades":         all_trades[-100:],   # most recent 100 trades
        "simulation":     _simulate_portfolio(signals, initial_usdt, buy_pct, sell_pct),
    }


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

                    # Check if any active trigger matches this signal
                    trigger_matched = False
                    if db is not None:
                        try:
                            active_triggers = await queries.get_triggers(db)
                            trigger_matched = any(
                                queries.trigger_matches(t, symbol, interval, signal.confidence, signal.adx_val)
                                for t in active_triggers
                            )
                        except Exception:
                            pass

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
                    }
                    await ws.send_json(signal_payload)

                    # Send Telegram alert — ONLY when a trigger matches this signal
                    if tg_bot is not None and settings is not None and trigger_matched:
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
