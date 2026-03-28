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
