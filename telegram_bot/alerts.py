"""
Signal alert dispatcher.

Phase 1: plain text alert — no action keyboard.
Phase 2: text + [Confirm BUY/SELL] [Ignore] keyboard (pre-calculated quantity).
Phase 3: informational "Auto-executed" message.
"""

from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from signals.detector import Signal


def _format_signal(signal: Signal, symbol: str, interval: str) -> str:
    icon = "🟢" if signal.direction == "BUY" else "🔴"
    conf_label = {"HIGH": "🔥 HIGH", "MEDIUM": "⚡ MEDIUM", "LOW": "LOW"}.get(signal.confidence, signal.confidence)
    reasons_str = "  ·  ".join(signal.reasons) if signal.reasons else "—"
    return (
        f"{icon} *{signal.direction} SIGNAL* — {conf_label}\n"
        f"Pair: `{symbol}` | `{interval}`\n"
        f"Entry: `${signal.entry_price:,.2f}`\n"
        f"ADX: `{signal.adx_val:.1f}`\n"
        f"Reasons: {reasons_str}\n"
        f"Trend: `{signal.trend_note}`"
    )


async def send_signal_alert(
    bot: Bot,
    chat_id: str,
    signal: Signal,
    symbol: str,
    interval: str,
    phase: int,
    db=None,
    exchange=None,
    settings=None,
    signal_id: Optional[int] = None,
) -> Optional[int]:
    """
    Send a signal alert to the Telegram chat.
    Returns the sent message id (useful for later editing in Phase 2).
    """
    text = _format_signal(signal, symbol, interval)

    if phase == 1:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
        return msg.message_id

    elif phase == 2 and signal.confidence == "HIGH":
        # Pre-calculate quantity from risk settings
        quantity = 0.0
        if exchange is not None and settings is not None:
            try:
                balances = await exchange.get_balances()
                usdt_bal = next(
                    (float(b["balance"]) for b in balances if b["currency"] == "USDT"),
                    0.0,
                )
                quantity = (usdt_bal * settings.max_position_pct) / signal.entry_price
            except Exception:
                quantity = 0.0

        if quantity > 0:
            from telegram_bot.keyboards import trade_confirmation_keyboard
            side = "buy" if signal.direction == "BUY" else "sell"
            kb   = trade_confirmation_keyboard(side, symbol, quantity, signal.entry_price)
            text += f"\n\nQty: `{quantity:.6f}` {symbol.replace('USDT','')}"
            msg  = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text + "\n\n_(Could not calculate quantity — check API credentials)_",
                parse_mode=ParseMode.MARKDOWN,
            )

        # Persist pending order record
        if db is not None and signal_id is not None and quantity > 0:
            from database import queries
            await queries.insert_order(
                db,
                symbol=symbol,
                side="buy" if signal.direction == "BUY" else "sell",
                order_type="market_order",
                quantity=quantity,
                price=signal.entry_price,
                signal_id=signal_id,
                telegram_msg_id=msg.message_id,
            )
        return msg.message_id

    elif phase == 2:
        # MEDIUM / LOW — plain alert, no keyboard
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
        return msg.message_id

    elif phase == 3:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text + "\n\n_Auto-executing…_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return msg.message_id

    return None


async def send_trade_notification(
    bot: Bot,
    chat_id: str,
    side: str,
    symbol: str,
    quantity: float,
    price: float,
    usdt_amount: float,
    trigger_id: int,
    pnl_pct: Optional[float] = None,
    reason: str = "signal",
) -> None:
    """
    Send a trade execution confirmation to the user's Telegram chat.

    side:        "BUY" or "SELL"
    reason:      "signal" | "stop_loss" | "take_profit"
    pnl_pct:     Only set for SELL — realised P&L percentage
    """
    if side.upper() == "BUY":
        icon = "✅"
        action_line = "*BUY Executed*"
    else:
        icon = "🔴"
        action_line = "*SELL Executed*"

    reason_label = {
        "stop_loss":   "🛑 Stop-Loss",
        "take_profit": "🎯 Take-Profit",
        "signal":      "📊 Signal",
    }.get(reason, reason)

    base_currency = symbol.upper().replace("USDT", "").replace("usdt", "")
    lines = [
        f"{icon} {action_line} — {reason_label}",
        f"Pair: `{symbol.upper()}`  |  Trigger: `#{trigger_id}`",
        f"Qty: `{quantity:.6f}` {base_currency}  @  `${price:,.4f}`",
        f"USDT: `${usdt_amount:,.2f}`",
    ]
    if pnl_pct is not None:
        pnl_icon = "🟢" if pnl_pct >= 0 else "🔴"
        lines.append(f"P&L: {pnl_icon} `{pnl_pct:+.2f}%`")

    try:
        await bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass  # never let notification failure break trade flow


async def send_trade_error(
    bot: Bot,
    chat_id: str,
    trigger_id: int,
    symbol: str,
    side: str,
    reason: str,
) -> None:
    """Notify user when auto-trade fails to execute."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ *Auto-trade Failed*\n"
                f"Trigger: `#{trigger_id}` | `{symbol.upper()}`\n"
                f"Side: `{side.upper()}`\n"
                f"Reason: {reason}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


async def send_daily_pnl_report(
    bot: Bot,
    chat_id: str,
    rows: list[dict],
    window_label: str,
) -> None:
    """
    Send a daily P&L summary grouped by trigger for a 10am→10am IST window.
    `rows` is the output of get_daily_pnl_by_trigger() — last row is the grand total (trigger_id=None).
    Raises on Telegram error so the caller can log it properly.
    """
    if not rows:
        text = f"📊 *Daily P&L Report*\n_{window_label}_\n\nNo trades executed in this window."
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"📊 *Daily P&L Report*\n_{window_label}_\n"]

    data_rows = [r for r in rows if r["trigger_id"] is not None]
    total_row = next((r for r in rows if r["trigger_id"] is None), None)

    for r in data_rows:
        true_pnl  = r.get("true_pnl_usdt") or 0.0
        avg_pct   = r.get("avg_pnl_pct")
        fee       = r.get("fee_usdt", 0.0) or 0.0
        cash_flow = r.get("net_pnl_usdt") or 0.0

        # Primary: realised P&L from completed sell trades
        if avg_pct is not None:
            pnl_icon = "🟢" if avg_pct >= 0 else "🔴"
            pnl_line = f"  Realised: {pnl_icon} *{avg_pct:+.2f}%* (≈${true_pnl:+.4f})"
        else:
            pnl_line = "  Realised: — (no completed sells yet)"

        # Cash flow note — only shown when buys and sells are unequal (open positions)
        cash_note = ""
        if r["buy_count"] != r["sell_count"]:
            cf_icon   = "🟢" if cash_flow >= 0 else "🔴"
            cash_note = f"\n  Cash flow: {cf_icon} ${cash_flow:+.4f} ({r['buy_count']}B/{r['sell_count']}S)"

        lines.append(
            f"*#{r['trigger_id']}* `{r['symbol']}` `{r['interval']}`\n"
            f"  {r['buy_count']} buys · {r['sell_count']} sells  Fees: -${fee:.4f}\n"
            f"{pnl_line}{cash_note}"
        )

    if total_row:
        total_true = total_row.get("true_pnl_usdt") or 0.0
        total_cf   = total_row.get("net_pnl_usdt")  or 0.0
        total_fee  = total_row.get("fee_usdt", 0.0) or 0.0
        true_icon  = "🟢" if total_true >= 0 else "🔴"
        cf_icon    = "🟢" if total_cf   >= 0 else "🔴"
        lines.append(
            f"{'─' * 22}\n"
            f"📋 *TOTAL*  {total_row['buy_count']} buys · {total_row['sell_count']} sells  "
            f"Fees: -${total_fee:.4f}\n"
            f"  Realised P&L: {true_icon} *${total_true:+.4f}*\n"
            f"  Cash flow: {cf_icon} ${total_cf:+.4f}"
        )

    lines.append("\n_Realised = per-sell P&L · Cash flow = sells received − buys paid_")
    text = "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


async def send_trigger_alert(
    bot: Bot,
    chat_id: str,
    action: str,
    symbol: str,
    interval: str,
    min_confidence: str,
) -> None:
    """Send a Telegram notification when a trigger is created, updated, or deleted."""
    icons = {
        "created":  "🔔",
        "updated":  "✏️",
        "deleted":  "🗑️",
        "enabled":  "✅",
        "disabled": "⏸",
    }
    icon = icons.get(action, "📌")
    text = (
        f"{icon} *Trigger {action.upper()}*\n"
        f"Pair: `{symbol}` | `{interval}`\n"
        f"Min confidence: `{min_confidence}`"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def send_futures_trade_notification(
    bot: Bot,
    chat_id: str,
    side: str,
    symbol: str,
    quantity: float,
    entry_price: float,
    leverage: int,
    margin_usdt: float,
    sl_price: float,
    tp_price: float,
    liq_price: float,
    trigger_id: int,
    reason: str = "signal",
) -> None:
    """Send a Telegram notification for a futures position opened/closed."""
    icon   = "🟢📈" if side == "long" else "🔴📉"
    sl_pct = abs(sl_price - entry_price) / entry_price * 100 * leverage
    tp_pct = abs(tp_price - entry_price) / entry_price * 100 * leverage
    notional = round(margin_usdt * leverage, 2)
    text = (
        f"{icon} *FUTURES {side.upper()}* — Trigger #{trigger_id}\n"
        f"Pair: `{symbol}` | `{leverage}x` leverage\n"
        f"Entry: `${entry_price:,.4f}`\n"
        f"Margin: `${margin_usdt:.2f}` → Notional: `${notional:.2f}`\n"
        f"SL: `${sl_price:,.4f}` _(−{sl_pct:.1f}% on margin)_\n"
        f"TP: `${tp_price:,.4f}` _(+{tp_pct:.1f}% on margin)_\n"
        f"Liq: `${liq_price:,.4f}`\n"
        f"Reason: _{reason}_"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        import logging
        logging.getLogger("futures_alert").warning("Futures Telegram alert failed: %s", exc)


async def send_futures_close_notification(
    bot: Bot,
    chat_id: str,
    side: str,
    symbol: str,
    quantity: float,
    entry_price: float,
    close_price: float,
    leverage: int,
    margin_usdt: float,
    pnl_pct: float,
    pnl_usdt: float,
    trigger_id: int,
    reason: str = "signal",
) -> None:
    icon = "✅" if pnl_usdt >= 0 else "❌"
    pnl_sign = "+" if pnl_usdt >= 0 else ""
    text = (
        f"{icon} *FUTURES CLOSED* ({side.upper()}) — Trigger #{trigger_id}\n"
        f"Pair: `{symbol}` | `{leverage}x`\n"
        f"Entry: `${entry_price:,.4f}` → Exit: `${close_price:,.4f}`\n"
        f"Margin: `${margin_usdt:.2f}`\n"
        f"P&L: *{pnl_sign}{pnl_pct:.2f}% on margin* (`{pnl_sign}${abs(pnl_usdt):.4f} USDT`)\n"
        f"Reason: _{reason}_"
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        import logging
        logging.getLogger("futures_alert").warning("Futures close Telegram alert failed: %s", exc)
