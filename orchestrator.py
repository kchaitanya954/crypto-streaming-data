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

async def _stream_pair(symbol: str, interval: str, db, tg_bot, exchange, settings) -> None:
    """
    Stream one symbol/interval pair forever, detect signals, and fire
    Telegram alerts whenever an active trigger matches.
    Restarts automatically on any error.
    """
    from streaming.stream import stream_klines, fetch_historical_klines
    from signals.detector import SignalDetector
    from database import queries
    from telegram_bot.alerts import send_signal_alert

    while True:
        try:
            detector = SignalDetector()
            historical = await fetch_historical_klines(symbol, interval, limit=201)
            detector.seed(historical)
            log.info("BG worker seeded  %s %s  (%d bars)", symbol.upper(), interval, len(historical))

            async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=False):
                if not kline.is_closed:
                    continue

                signal = detector.update(kline)
                if signal is None:
                    continue

                log.info("BG signal  %s %s %s %s @ %.2f",
                         symbol.upper(), interval, signal.direction,
                         signal.confidence, signal.entry_price)

                # Persist to DB
                try:
                    await queries.insert_signal(db, symbol, interval, signal)
                except Exception:
                    pass

                # Fire Telegram alert if any active trigger matches
                try:
                    active_triggers = await queries.get_triggers(db, active_only=True)
                    matched = any(
                        queries.trigger_matches(t, symbol, interval, signal.confidence)
                        for t in active_triggers
                    )
                    if matched:
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
                        log.info("Trigger alert sent  %s %s %s %s",
                                 signal.direction, signal.confidence,
                                 symbol.upper(), interval)
                except Exception as exc:
                    log.error("BG trigger check error: %s", exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("BG worker %s %s crashed (%s) — restarting in 10 s",
                        symbol.upper(), interval, exc)
            await asyncio.sleep(10)


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
            pairs = {(t["symbol"].lower(), t["interval"]) for t in triggers}

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
