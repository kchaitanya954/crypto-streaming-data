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
        action_line = f"*BUY Executed*"
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
