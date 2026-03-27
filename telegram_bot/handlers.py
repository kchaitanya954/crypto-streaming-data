"""
Telegram command handlers and callback query handlers.

Commands:
  /start   — Welcome message + command list
  /status  — CoinDCX balances + last 3 signals
  /buy     — Place a manual buy order (with confirmation keyboard)
  /sell    — Place a manual sell order (with confirmation keyboard)
  /history — Last 10 trade fills with P&L

Callbacks:
  confirm_<side>_<market>_<qty>_<price>  — Execute trade
  cancel_<side>_<market>                 — Ignore signal
"""

import time
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from telegram_bot.keyboards import trade_confirmation_keyboard


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.bot_data.get("db")

def _exchange(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.bot_data.get("exchange")

def _settings(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.bot_data.get("settings")


# ── /start ────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Crypto Trading Bot*\n\n"
        "Commands:\n"
        "/status — balances + recent signals\n"
        "/buy `MARKET QTY PRICE` — place a buy order\n"
        "  e.g. `/buy BTCUSDT 0.001 67000`\n"
        "/sell `MARKET QTY PRICE` — place a sell order\n"
        "  e.g. `/sell BTCUSDT 0.001 68000`\n"
        "/history — recent trade fills\n\n"
        "_Market orders: omit PRICE or use `market`_",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    exchange = _exchange(ctx)
    db       = _db(ctx)
    settings = _settings(ctx)

    lines = ["*Portfolio*"]
    if exchange:
        try:
            balances = await exchange.get_balances()
            for b in balances[:10]:
                avail  = float(b.get("balance", 0))
                locked = float(b.get("locked_balance", 0))
                total  = avail + locked
                if total < 0.000001:
                    continue
                lock_str = f" (🔒 {locked:.6f})" if locked > 0 else ""
                lines.append(f"  `{b['currency']}`: {total:.6f}{lock_str}")
        except Exception as e:
            lines.append(f"  _Error: {e}_")
    else:
        lines.append("  _Exchange not configured_")

    if db and settings:
        try:
            from database import queries
            signals = await queries.get_recent_signals(
                db, symbol=settings.symbol, limit=3
            )
            if signals:
                lines.append("\n*Recent Signals*")
                for s in signals:
                    ts  = time.strftime("%H:%M %d/%m", time.localtime(s["created_at"]))
                    lines.append(
                        f"  {'🟢' if s['direction'] == 'BUY' else '🔴'} "
                        f"{s['direction']} {s['confidence']} @ "
                        f"`${s['entry_price']:,.2f}` [{ts}]"
                    )
        except Exception:
            pass

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /buy and /sell ────────────────────────────────────────────────────────────

async def _handle_trade_command(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    side: str,
) -> None:
    """Shared handler for /buy and /sell."""
    args = ctx.args or []

    if len(args) < 2:
        await update.message.reply_text(
            f"Usage: `/{side} MARKET QTY [PRICE]`\n"
            f"Example: `/{side} BTCUSDT 0.001 67000`\n"
            f"Market order: `/{side} BTCUSDT 0.001 market`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    market = args[0].upper()
    try:
        quantity = float(args[1])
    except ValueError:
        await update.message.reply_text("Invalid quantity.", parse_mode=ParseMode.MARKDOWN)
        return

    price_arg = args[2] if len(args) >= 3 else "market"
    is_market_order = price_arg.lower() == "market" or price_arg == ""

    price: Optional[float] = None
    order_type = "market_order"
    if not is_market_order:
        try:
            price = float(price_arg)
            order_type = "limit_order"
        except ValueError:
            await update.message.reply_text("Invalid price.", parse_mode=ParseMode.MARKDOWN)
            return

    display_price = price if price else "market"
    text = (
        f"{'🟢 BUY' if side == 'buy' else '🔴 SELL'} Order Confirmation\n\n"
        f"Market: `{market}`\n"
        f"Quantity: `{quantity}`\n"
        f"Price: `{display_price}`\n"
        f"Type: `{order_type}`"
    )

    kb = trade_confirmation_keyboard(
        side=side,
        market=market,
        quantity=quantity,
        price=price if price else 0.0,
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_trade_command(update, ctx, "buy")


async def cmd_sell(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_trade_command(update, ctx, "sell")


# ── /history ──────────────────────────────────────────────────────────────────

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(ctx)
    if db is None:
        await update.message.reply_text("_Database not configured._", parse_mode=ParseMode.MARKDOWN)
        return

    from database import queries
    trades = await queries.get_trade_history(db, limit=10)

    if not trades:
        await update.message.reply_text("No trade history yet.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["*Recent Trades*"]
    for t in trades:
        ts   = time.strftime("%d/%m %H:%M", time.localtime(t["created_at"]))
        pnl  = f"  P&L: `{t['pnl']:+.4f}`" if t.get("pnl") is not None else ""
        icon = "🟢" if t["side"] == "buy" else "🔴"
        lines.append(
            f"{icon} {t['side'].upper()} `{t['symbol']}` "
            f"{t['filled_qty']} @ `${t['filled_price']:,.2f}`"
            f"{pnl} [{ts}]"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Callback: confirm trade ───────────────────────────────────────────────────

async def on_confirm_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # Format: confirm_<side>_<market>_<qty>_<price>
    parts = query.data.split("_", 4)  # ["confirm", side, market, qty, price]
    if len(parts) < 5:
        await query.edit_message_text("Invalid callback data.")
        return

    _, side, market, qty_str, price_str = parts
    try:
        quantity = float(qty_str)
        price    = float(price_str)
    except ValueError:
        await query.edit_message_text("Invalid order parameters.")
        return

    exchange = _exchange(ctx)
    db       = _db(ctx)

    if exchange is None:
        await query.edit_message_text("Exchange not configured. Cannot place order.")
        return

    order_type = "limit_order" if price > 0 else "market_order"

    try:
        result = await exchange.create_order(
            side=side,
            market=market,
            order_type=order_type,
            quantity=quantity,
            price=price if price > 0 else None,
        )
        order_id = result.get("id", "unknown")

        # Persist to DB
        if db is not None:
            from database import queries
            row_id = await queries.insert_order(
                db,
                symbol=market,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price if price > 0 else None,
                telegram_msg_id=query.message.message_id,
            )
            await queries.update_order_status(db, row_id, "open", str(order_id))

        await query.edit_message_text(
            f"{'🟢 BUY' if side == 'buy' else '🔴 SELL'} Order Placed\n\n"
            f"Market: `{market}`\n"
            f"Qty: `{quantity}`\n"
            f"CoinDCX ID: `{order_id}`\n"
            f"Status: open",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await query.edit_message_text(
            f"Order failed: `{e}`",
            parse_mode=ParseMode.MARKDOWN,
        )


# ── Callback: cancel trade ────────────────────────────────────────────────────

async def on_cancel_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        query.message.text + "\n\n_Signal ignored._",
        parse_mode=ParseMode.MARKDOWN,
    )
