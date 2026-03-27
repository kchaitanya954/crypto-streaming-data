"""InlineKeyboardMarkup builders for trade confirmation flows."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def trade_confirmation_keyboard(
    side: str,
    market: str,
    quantity: float,
    price: float,
) -> InlineKeyboardMarkup:
    """
    Keyboard for Phase 2 signal approval.

    Callback data format: confirm_<side>_<market>_<qty>_<price>
    e.g. confirm_buy_BTCUSDT_0.00050_67432.10
    """
    cb_confirm = f"confirm_{side}_{market}_{quantity:.8f}_{price:.2f}"
    cb_cancel  = f"cancel_{side}_{market}"
    label = "Confirm BUY" if side == "buy" else "Confirm SELL"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label,   callback_data=cb_confirm),
        InlineKeyboardButton("Ignore", callback_data=cb_cancel),
    ]])


def order_placed_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Keyboard shown after an order is placed (link to history)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("View history", callback_data=f"history_{order_id}"),
    ]])
