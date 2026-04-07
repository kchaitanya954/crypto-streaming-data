"""
Telegram command handlers and callback query handlers.

Commands:
  /start        — Welcome message + command list
  /status       — CoinDCX balances + last 3 signals
  /portfolio    — Full portfolio breakdown
  /triggers     — List all triggers
  /addtrigger   — Create a trigger
  /edittrigger  — Edit a trigger
  /deltrigger   — Delete a trigger
  /toggletrigger— Enable/disable a trigger
  /buy          — Place a manual buy order
  /sell         — Place a manual sell order
  /history      — Last 10 trade fills with P&L

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

_SENTINEL_ = object()   # distinguishes "not provided" from None in edit commands


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
        "<b>Crypto Trading Bot</b>\n\n"
        "<b>Portfolio</b>\n"
        "/status — balances + recent signals\n"
        "/portfolio — full portfolio breakdown\n\n"
        "<b>Triggers</b>\n"
        "/triggers — list all triggers\n"
        "/addtrigger <code>SYMBOL INTERVAL [CONF] [ADX] [COOLDOWN]</code>\n"
        "  e.g. <code>/addtrigger BTCUSDT 15m HIGH 20 5</code>\n"
        "/edittrigger <code>ID [CONF] [ADX] [COOLDOWN]</code>\n"
        "  e.g. <code>/edittrigger 3 HIGH 25 3</code>  (use - to skip a field)\n"
        "/deltrigger <code>ID</code> — delete trigger\n"
        "/toggletrigger <code>ID</code> — enable / disable trigger\n\n"
        "<b>Trading</b>\n"
        "/buy <code>MARKET QTY PRICE</code> — place a buy order\n"
        "  e.g. <code>/buy BTCUSDT 0.001 67000</code>\n"
        "/sell <code>MARKET QTY PRICE</code> — place a sell order\n"
        "/history — recent trade fills\n\n"
        "<i>Market orders: omit PRICE or use market</i>",
        parse_mode=ParseMode.HTML,
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


# ── /portfolio ────────────────────────────────────────────────────────────────

async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Full portfolio: all non-zero balances with USDT equivalent estimates."""
    exchange = _exchange(ctx)
    if exchange is None:
        await update.message.reply_text("_Exchange not configured._", parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["*Portfolio (CoinDCX)*"]
    try:
        balances = await exchange.get_balances()
        non_zero = [b for b in balances if float(b.get("balance", 0)) + float(b.get("locked_balance", 0)) > 1e-8]
        if not non_zero:
            lines.append("  _No balances found._")
        else:
            for b in non_zero:
                avail  = float(b.get("balance", 0))
                locked = float(b.get("locked_balance", 0))
                total  = avail + locked
                lock_str = f" 🔒{locked:.6f}" if locked > 0 else ""
                lines.append(f"  `{b['currency']}` {total:.6f}{lock_str}")
    except Exception as e:
        lines.append(f"  _Error fetching balances: {e}_")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── /triggers ─────────────────────────────────────────────────────────────────

def _fmt_trigger(t: dict) -> str:
    """Format one trigger row as an HTML line."""
    adx_str = f" adx≥{t['adx_threshold']}" if t.get("adx_threshold") else ""
    cd_str  = f" cd={t['cooldown_bars']}"   if t.get("cooldown_bars") is not None else ""
    return (
        f"  <code>#{t['id']}</code> {t['symbol']} {t['interval']} "
        f"{t['min_confidence']}{adx_str}{cd_str}"
    )


async def cmd_triggers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """List all triggers (active and inactive) with their settings."""
    db = _db(ctx)
    if db is None:
        await update.message.reply_text("Database not configured.")
        return

    try:
        from database import queries
        triggers = await queries.get_triggers(db, active_only=False)
    except Exception as e:
        await update.message.reply_text(f"Error fetching triggers: {e}")
        return

    if not triggers:
        await update.message.reply_text(
            "No triggers configured.\n"
            "Use /addtrigger SYMBOL INTERVAL [CONF] [ADX] [COOLDOWN] to create one."
        )
        return

    active   = [t for t in triggers if t.get("active")]
    inactive = [t for t in triggers if not t.get("active")]

    lines = [f"<b>Triggers ({len(triggers)} total)</b>"]
    if active:
        lines.append("\n✅ <b>Active</b>")
        lines.extend(_fmt_trigger(t) for t in active)
    if inactive:
        lines.append("\n⏸ <b>Inactive</b>")
        lines.extend(_fmt_trigger(t) for t in inactive)
    lines.append(
        "\n<i>/addtrigger  /edittrigger  /deltrigger  /toggletrigger</i>"
    )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── /addtrigger ───────────────────────────────────────────────────────────────

async def cmd_addtrigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /addtrigger SYMBOL INTERVAL [CONF=MEDIUM] [ADX] [COOLDOWN]
    e.g. /addtrigger BTCUSDT 15m
         /addtrigger BTCUSDT 15m HIGH 20 5
    Use - to leave ADX or COOLDOWN at tier default.
    """
    db   = _db(ctx)
    args = ctx.args or []

    if db is None:
        await update.message.reply_text("Database not configured.")
        return
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: <code>/addtrigger SYMBOL INTERVAL [CONF] [ADX] [COOLDOWN]</code>\n"
            "Example: <code>/addtrigger BTCUSDT 15m HIGH 20 5</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    symbol   = args[0].upper()
    interval = args[1].lower()
    conf     = (args[2].upper() if len(args) > 2 and args[2] != "-" else "MEDIUM")
    if conf not in ("LOW", "MEDIUM", "HIGH"):
        conf = "MEDIUM"

    adx_threshold: Optional[float] = None
    cooldown_bars: Optional[int]   = None
    try:
        if len(args) > 3 and args[3] != "-":
            adx_threshold = float(args[3])
        if len(args) > 4 and args[4] != "-":
            cooldown_bars = int(args[4])
    except ValueError:
        await update.message.reply_text("Invalid ADX or cooldown value.")
        return

    try:
        from database import queries
        new_id = await queries.create_trigger(
            db, symbol=symbol, interval=interval,
            min_confidence=conf,
            adx_threshold=adx_threshold,
            cooldown_bars=cooldown_bars,
        )
        adx_str = f" adx≥{adx_threshold}" if adx_threshold else " (tier default ADX)"
        cd_str  = f" cooldown={cooldown_bars}" if cooldown_bars is not None else ""
        await update.message.reply_text(
            f"✅ Trigger <b>#{new_id}</b> created\n"
            f"<code>{symbol} {interval} {conf}{adx_str}{cd_str}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to create trigger: {e}")


# ── /edittrigger ──────────────────────────────────────────────────────────────

async def cmd_edittrigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /edittrigger ID [CONF] [ADX] [COOLDOWN]
    e.g. /edittrigger 3 HIGH 25 3
    Use - to leave a field unchanged.
    """
    db   = _db(ctx)
    args = ctx.args or []

    if db is None:
        await update.message.reply_text("Database not configured.")
        return
    if not args:
        await update.message.reply_text(
            "Usage: <code>/edittrigger ID [CONF] [ADX] [COOLDOWN]</code>\n"
            "Example: <code>/edittrigger 3 HIGH 25 3</code>  (use - to skip a field)",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        trigger_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Trigger ID must be a number.")
        return

    from database import queries
    trig = await queries.get_trigger(db, trigger_id)
    if not trig:
        await update.message.reply_text(f"Trigger #{trigger_id} not found.")
        return

    # Parse optional fields (- means keep current value)
    conf: Optional[str]   = None
    adx:  Optional[float] = _SENTINEL_  # use sentinel to distinguish "not given" from None
    cd:   Optional[int]   = _SENTINEL_

    if len(args) > 1 and args[1] != "-":
        v = args[1].upper()
        if v in ("LOW", "MEDIUM", "HIGH"):
            conf = v
    if len(args) > 2 and args[2] != "-":
        try:
            adx = float(args[2])
        except ValueError:
            await update.message.reply_text("Invalid ADX value.")
            return
    elif len(args) > 2 and args[2] == "-":
        adx = None   # explicitly clear it
    if len(args) > 3 and args[3] != "-":
        try:
            cd = int(args[3])
        except ValueError:
            await update.message.reply_text("Invalid cooldown value.")
            return
    elif len(args) > 3 and args[3] == "-":
        cd = None

    kwargs: dict = {}
    if conf is not None:
        kwargs["min_confidence"] = conf
    if adx is not _SENTINEL_:
        kwargs["adx_threshold"] = adx
    if cd is not _SENTINEL_:
        kwargs["cooldown_bars"] = cd

    if not kwargs:
        await update.message.reply_text("Nothing to update. Provide at least one field.")
        return

    try:
        await queries.update_trigger(db, trigger_id, **kwargs)
        updated = await queries.get_trigger(db, trigger_id)
        await update.message.reply_text(
            f"✏️ Trigger <b>#{trigger_id}</b> updated\n{_fmt_trigger(updated)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to update trigger: {e}")


# ── /deltrigger ───────────────────────────────────────────────────────────────

async def cmd_deltrigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/deltrigger ID — delete a trigger permanently."""
    db   = _db(ctx)
    args = ctx.args or []

    if db is None:
        await update.message.reply_text("Database not configured.")
        return
    if not args:
        await update.message.reply_text(
            "Usage: <code>/deltrigger ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        trigger_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Trigger ID must be a number.")
        return

    from database import queries
    trig = await queries.get_trigger(db, trigger_id)
    if not trig:
        await update.message.reply_text(f"Trigger #{trigger_id} not found.")
        return

    try:
        await queries.delete_trigger(db, trigger_id)
        await update.message.reply_text(
            f"🗑️ Trigger <b>#{trigger_id}</b> deleted "
            f"({trig['symbol']} {trig['interval']} {trig['min_confidence']})",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to delete trigger: {e}")


# ── /toggletrigger ────────────────────────────────────────────────────────────

async def cmd_toggletrigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/toggletrigger ID — enable if inactive, disable if active."""
    db   = _db(ctx)
    args = ctx.args or []

    if db is None:
        await update.message.reply_text("Database not configured.")
        return
    if not args:
        await update.message.reply_text(
            "Usage: <code>/toggletrigger ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        trigger_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Trigger ID must be a number.")
        return

    from database import queries
    trig = await queries.get_trigger(db, trigger_id)
    if not trig:
        await update.message.reply_text(f"Trigger #{trigger_id} not found.")
        return

    new_active = not bool(trig.get("active"))
    try:
        await queries.update_trigger(db, trigger_id, active=new_active)
        icon = "✅" if new_active else "⏸"
        state = "enabled" if new_active else "disabled"
        await update.message.reply_text(
            f"{icon} Trigger <b>#{trigger_id}</b> {state} "
            f"({trig['symbol']} {trig['interval']})",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Failed to toggle trigger: {e}")


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
