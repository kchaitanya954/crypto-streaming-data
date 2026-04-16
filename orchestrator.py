"""
Main orchestrator — starts all services concurrently:
  1. SQLite database
  2. CoinDCX exchange client (order execution)
  3. Telegram bot (alerts + manual trading)
  4. FastAPI + Uvicorn (dashboard + WebSocket streaming)
  5. Background signal worker (detects signals & fires trigger alerts 24/7)

Run:
    python orchestrator.py

.env file must exist with all required keys (see .env.example).
"""

import asyncio
import logging

import aiohttp
import uvicorn

from config import load_settings
from database.db import init_db
from exchange.coindcx_client import CoinDCXClient
from telegram_bot.bot import create_bot
from ui.server import app as fastapi_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("orchestrator")


# ── Background signal worker ───────────────────────────────────────────────────

async def _check_sl_tp(symbol: str, current_price: float, db, settings) -> None:
    """
    After every closed candle, check all open positions on this symbol against
    their stop-loss / take-profit prices and auto-sell when triggered.
    """
    from database import queries
    from exchange.auto_trade import execute_trigger_trade
    from signals.detector import Signal
    import time as _time

    positions = await queries.get_positions_for_sl_tp_check(db, symbol)
    for pos in positions:
        trigger_id = pos["trigger_id"]
        sl_price   = pos.get("stop_loss_price")
        tp_price   = pos.get("take_profit_price")

        reason = None
        if sl_price and current_price <= sl_price:
            reason = "stop_loss"
        elif tp_price and current_price >= tp_price:
            reason = "take_profit"

        if not reason:
            continue

        log.info(
            "SL/TP triggered: trigger=%d symbol=%s price=%.4f reason=%s (SL=%.4f TP=%.4f)",
            trigger_id, symbol.upper(), current_price, reason,
            sl_price or 0, tp_price or 0,
        )

        # Build a minimal synthetic signal for the SELL executor
        trigger = await queries.get_trigger(db, trigger_id)
        if not trigger or not trigger.get("active"):
            continue

        fake_signal = Signal(
            direction="SELL",
            confidence="HIGH",
            entry_price=current_price,
            open_time=int(_time.time() * 1000),
            macd_val=0.0, signal_val=0.0, histogram=0.0,
            adx_val=0.0, trend_note=reason,
            reasons=[reason],
        )

        asyncio.create_task(
            execute_trigger_trade(db, trigger, fake_signal, symbol.upper(),
                                  trigger["interval"], signal_id=None, reason=reason)
        )


async def _stream_pair(symbol: str, interval: str, db, tg_bot, exchange, settings) -> None:
    """
    Stream one symbol/interval pair forever, detect signals, and fire
    Telegram alerts whenever an active trigger matches.
    Restarts automatically on any error with exponential backoff.
    """
    from streaming.stream import stream_klines, fetch_historical_klines
    from signals.detector import SignalDetector, params_for_interval
    from database import queries
    from telegram_bot.alerts import send_signal_alert

    retry_delay = 10  # seconds; doubles on each crash, capped at 300 s

    while True:
        try:
            detector = SignalDetector(**params_for_interval(interval))
            log.info("BG worker params  %s %s  tier=%s",
                     symbol.upper(), interval,
                     "scalping" if params_for_interval(interval)["adx_threshold"] <= 12 else
                     "intraday" if params_for_interval(interval)["adx_threshold"] <= 18 else
                     "swing"    if params_for_interval(interval)["adx_threshold"] <= 20 else
                     "position")
            historical = await fetch_historical_klines(symbol, interval, limit=201)
            detector.seed(historical)
            log.info("BG worker seeded  %s %s  (%d bars)", symbol.upper(), interval, len(historical))

            candle_count = 0
            async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=False):
                if not kline.is_closed:
                    continue

                candle_count += 1
                signal = detector.update(kline)
                # Log every 30 candles so we can confirm the worker is alive and processing
                if candle_count % 30 == 0:
                    snap = detector.current_snapshot()
                    log.info("BG heartbeat  %s %s  candle#%d  close=%.4f  adx=%s",
                             symbol.upper(), interval, candle_count, kline.close,
                             f"{snap.adx_val:.1f}" if snap.adx_val else "n/a")

                # ── Stop-loss / take-profit check (every candle) ─────────────
                try:
                    await _check_sl_tp(symbol, kline.close, db, settings)
                except Exception as exc:
                    log.error("SL/TP check error %s: %s", symbol.upper(), exc)

                if signal is None:
                    continue

                log.info("BG signal  %s %s %s %s @ %.2f",
                         symbol.upper(), interval, signal.direction,
                         signal.confidence, signal.entry_price)

                # Persist to DB and capture signal_id for trade linkage
                signal_id = None
                try:
                    signal_id = await queries.insert_signal(db, symbol, interval, signal)
                except Exception:
                    pass

                # Fire Telegram alert and execute auto-trades for matched triggers
                try:
                    from exchange.auto_trade import execute_trigger_trade
                    active_triggers = await queries.get_triggers(db, active_only=True)
                    matched_triggers = [
                        t for t in active_triggers
                        if t.get("user_id") is not None
                        and queries.trigger_matches(t, symbol, interval, signal.confidence, signal.adx_val)
                    ]
                    if matched_triggers:

                        # Send Telegram alert once (uses first matched trigger's user settings)
                        try:
                            user_settings = await queries.get_user_settings(db, matched_triggers[0]["user_id"])
                            from auth.encryption import safe_decrypt
                            tg_token   = safe_decrypt((user_settings or {}).get("telegram_token"))
                            tg_chat_id = safe_decrypt((user_settings or {}).get("telegram_chat_id"))
                            if tg_token and tg_chat_id:
                                from telegram import Bot
                                per_user_bot = Bot(token=tg_token)
                                await send_signal_alert(
                                    bot=per_user_bot,
                                    chat_id=tg_chat_id,
                                    signal=signal,
                                    symbol=symbol.upper(),
                                    interval=interval,
                                    phase=settings.trading_phase,
                                    db=db,
                                    exchange=exchange,
                                    settings=settings,
                                )
                            else:
                                # Fall back to global bot
                                await send_signal_alert(
                                    bot=tg_bot,
                                    chat_id=settings.telegram_chat_id,
                                    signal=signal,
                                    symbol=symbol.upper(),
                                    interval=interval,
                                    phase=settings.trading_phase,
                                    db=db,
                                    exchange=exchange,
                                    settings=settings,
                                )
                        except Exception as exc:
                            log.error("BG Telegram alert error: %s", exc)

                        # Execute trade for each matched trigger
                        for trig in matched_triggers:
                            asyncio.create_task(
                                execute_trigger_trade(
                                    db, trig, signal, symbol.upper(), interval, signal_id
                                )
                            )
                            log.info("BG auto-trade queued  trigger=%d  %s %s %s %s",
                                     trig["id"], signal.direction, signal.confidence,
                                     symbol.upper(), interval)
                except Exception as exc:
                    log.error("BG trigger check error: %s", exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("BG worker %s %s crashed (%s) — restarting in %ds",
                        symbol.upper(), interval, exc, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)  # exponential backoff, cap 5 min
            continue

        # Successful run — reset backoff
        retry_delay = 10


async def background_signal_worker(db, tg_bot, exchange, settings) -> None:
    """
    Manage one streaming task per unique (symbol, interval) across all active
    triggers.  Re-checks the DB every 30 s so newly added/removed triggers are
    picked up without a restart.
    """
    from database import queries

    running: dict[tuple, asyncio.Task] = {}

    while True:
        try:
            triggers = await queries.get_triggers(db, active_only=True)
            # Only stream pairs for triggers that belong to a real user
            pairs = {
                (t["symbol"].lower(), t["interval"])
                for t in triggers
                if t.get("user_id") is not None
            }

            # Start tasks for new pairs
            for sym, iv in pairs:
                key = (sym, iv)
                if key not in running or running[key].done():
                    log.info("BG worker starting %s %s", sym.upper(), iv)
                    running[key] = asyncio.create_task(
                        _stream_pair(sym, iv, db, tg_bot, exchange, settings)
                    )

            # Cancel tasks for pairs that no longer have active triggers
            for key in list(running):
                if key not in pairs and not running[key].done():
                    log.info("BG worker stopping %s %s", key[0].upper(), key[1])
                    running[key].cancel()
                    del running[key]

        except asyncio.CancelledError:
            for task in running.values():
                task.cancel()
            raise
        except Exception as exc:
            log.error("BG worker manager error: %s", exc)

        await asyncio.sleep(30)   # re-sync with DB triggers every 30 s


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # ── Load config ──────────────────────────────────────────────────────────
    settings = load_settings()
    log.info("Config loaded — phase=%d  symbol=%s  interval=%s",
             settings.trading_phase, settings.symbol, settings.interval)

    # ── Database ─────────────────────────────────────────────────────────────
    db = await init_db(settings.db_path)
    log.info("SQLite ready at %s", settings.db_path)

    # ── HTTP session (shared across exchange + any async HTTP calls) ──────────
    session = aiohttp.ClientSession()

    # ── CoinDCX client ────────────────────────────────────────────────────────
    exchange = CoinDCXClient(
        api_key=settings.coindcx_api_key,
        api_secret=settings.coindcx_api_secret,
        session=session,
    )
    log.info("CoinDCX client ready")

    # ── Telegram bot ──────────────────────────────────────────────────────────
    tg_app = await create_bot(settings, db, exchange)
    log.info("Telegram bot configured")

    # ── Inject dependencies into FastAPI app.state ───────────────────────────
    fastapi_app.state.db            = db
    fastapi_app.state.exchange      = exchange
    fastapi_app.state.telegram_bot  = tg_app.bot
    fastapi_app.state.settings      = settings

    # ── Start everything ──────────────────────────────────────────────────────
    uvicorn_config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=settings.port,
        log_level="warning",
    )
    server = uvicorn.Server(uvicorn_config)

    log.info("Starting dashboard on http://0.0.0.0:%d", settings.port)
    log.info("Starting Telegram bot polling")

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)

        # Start background signal worker (runs forever, independent of UI)
        worker_task = asyncio.create_task(
            background_signal_worker(db, tg_app.bot, exchange, settings)
        )
        log.info("Background signal worker started")

        try:
            await server.serve()
        finally:
            log.info("Shutting down…")
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
            await tg_app.updater.stop()
            await tg_app.stop()

    await session.close()
    await db.close()
    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
