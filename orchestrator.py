"""
Main orchestrator — starts all services concurrently:
  1. SQLite database
  2. CoinDCX exchange client (order execution)
  3. Telegram bot (alerts + manual trading)
  4. FastAPI + Uvicorn (dashboard + WebSocket streaming)

Run:
    python orchestrator.py

.env file must exist with all required keys (see .env.example).
"""

import asyncio
import logging
import os

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
log = logging.getLogger("orchestrator")


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

        try:
            await server.serve()
        finally:
            log.info("Shutting down…")
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
