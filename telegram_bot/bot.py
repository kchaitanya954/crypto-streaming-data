"""
Telegram Application setup and handler registration.

Usage:
    from telegram_bot import create_bot
    tg_app = await create_bot(settings, db, exchange)
    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling()
        # ... run other tasks ...
        await tg_app.updater.stop()
        await tg_app.stop()
"""

from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
)

from telegram_bot import handlers


async def create_bot(settings, db, exchange) -> Application:
    """Build and configure the Telegram Application."""
    app = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Inject shared state into bot_data so handlers can access them
    app.bot_data["db"]       = db
    app.bot_data["exchange"] = exchange
    app.bot_data["settings"] = settings

    # Command handlers
    app.add_handler(CommandHandler("start",         handlers.cmd_start))
    app.add_handler(CommandHandler("status",        handlers.cmd_status))
    app.add_handler(CommandHandler("portfolio",     handlers.cmd_portfolio))
    app.add_handler(CommandHandler("triggers",      handlers.cmd_triggers))
    app.add_handler(CommandHandler("addtrigger",    handlers.cmd_addtrigger))
    app.add_handler(CommandHandler("edittrigger",   handlers.cmd_edittrigger))
    app.add_handler(CommandHandler("deltrigger",    handlers.cmd_deltrigger))
    app.add_handler(CommandHandler("toggletrigger", handlers.cmd_toggletrigger))
    app.add_handler(CommandHandler("buy",           handlers.cmd_buy))
    app.add_handler(CommandHandler("sell",          handlers.cmd_sell))
    app.add_handler(CommandHandler("history",       handlers.cmd_history))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handlers.on_confirm_trade, pattern=r"^confirm_"))
    app.add_handler(CallbackQueryHandler(handlers.on_cancel_trade,  pattern=r"^cancel_"))

    return app
