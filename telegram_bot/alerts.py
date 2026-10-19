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


def _pair_label(pair: str) -> str:
    """B-BTC_USDT → BTC (underscores break Telegram Markdown)."""
    p = (pair or "?").replace("B-", "")
    return p.split("_")[0] if "_" in p else p


def _money(v: float, cur: str = "USDT") -> str:
    return f"₹{v:+,.2f}" if cur == "INR" else f"${v:+.4f}"


def format_exchange_section(ex: dict) -> list[str]:
    """Exchange-ledger section of the daily report (see exchange.account_ledger)."""
    lines = ["🏦 *Exchange (CoinDCX ledger — source of truth)*"]
    any_activity = False
    for cur in ("USDT", "INR"):
        fut = ex.get(f"futures_{cur.lower()}")
        if not fut or not fut["pairs"]:
            continue
        any_activity = True
        t = fut["total"]
        icon = "🟢" if t["net"] >= 0 else "🔴"
        lines.append(f"Futures ({cur}): {icon} *{_money(t['net'], cur)}* net · "
                     f"{t['closes']} closes ({t['wins']}W/{t['losses']}L)")
        lines.append(f"  P&L {_money(t['pnl'], cur)} · fees -{abs(t['fees']):.4f} · funding {t['funding']:+.4f}")
        for pair, p in sorted(ex[f"futures_{cur.lower()}"]["pairs"].items()):
            liq = f" ⚠️ {p['liquidations']} LIQUIDATED" if p["liquidations"] else ""
            lines.append(f"  `{_pair_label(pair)}` {_money(p['net'], cur)} ({p['closes']} closes){liq}")
    spot = ex.get("spot")
    if spot and spot["markets"]:
        any_activity = True
        t = spot["total"]
        icon = "🟢" if t["net"] >= 0 else "🔴"
        lines.append(f"Spot: {icon} *≈${t['net']:+.4f}* net · {t['buys']}B/{t['sells']}S · "
                     f"vol ${t['volume']:.2f} · fees -${t['fees']:.4f}")
    if not any_activity:
        lines.append("No fills on the exchange in this window.")
    for p in ex.get("open_positions", []):
        guard = f"SL {p['sl']:,.2f}" if p["sl"] else "⚠️ *NO STOP-LOSS*"
        lines.append(f"Open: `{_pair_label(p['pair'])}` {p['side'].upper()} {p['qty']:g} @ {p['avg']:,.2f} "
                     f"(mark {p['mark']:,.2f}, uP&L {p['upnl']:+.4f}) · {guard}")
    w = ex.get("wallets")
    if w:
        lines.append("Futures wallet: " + " · ".join(f"{k} {v:,.4f}" for k, v in sorted(w.items())))
    for err in ex.get("errors", []):
        lines.append(f"  _(unavailable: {err.replace('_', ' ')})_")
    return lines


def format_strategy_section(rows: list[dict]) -> list[str]:
    """ICT/SMT strategy trades (paper and live) that closed or are still active in the window."""
    if not rows:
        return []
    lines = ["🗽 *ICT / SMT strategy*"]
    for mode in ("live", "paper"):
        rs = [r for r in rows if r["mode"] == mode]
        if not rs:
            continue
        closed = [r for r in rs if r["status"] == "closed"]
        total_r = sum(r["r_multiple"] or 0 for r in closed)
        pnl = sum(r["pnl_usdt"] or 0 for r in closed)
        wins = sum(1 for r in closed if (r["pnl_usdt"] or 0) > 0)
        tag = "LIVE" if mode == "live" else "PAPER (no real money)"
        lines.append(f"{tag}: {len(closed)} closed ({wins}W/{len(closed) - wins}L) · "
                     f"{total_r:+.2f}R · ${pnl:+.4f}")
        for r in rs:
            if r["status"] == "closed":
                lines.append(f"  #{r['trigger_id']} {r['side'].upper()} `{r['symbol']}` "
                             f"{(r['exit_reason'] or '').replace('_', ' ')} {r['r_multiple']:+.2f}R")
            else:
                lines.append(f"  #{r['trigger_id']} {r['side'].upper()} `{r['symbol']}` {r['status']}")
    return lines


async def send_daily_pnl_report(
    bot: Bot,
    chat_id: str,
    rows: list[dict],
    window_label: str,
    exchange: Optional[dict] = None,
    strategy_rows: Optional[list[dict]] = None,
) -> None:
    """
    Send a daily P&L summary for a 10am→10am IST window.

    `exchange` (exchange.account_ledger.exchange_window_summary) comes first: it is
    what CoinDCX actually booked, including exchange-side SL/TP fills, funding,
    manual trades and liquidations that the bot's own tables never see.
    `rows` is get_daily_pnl_by_trigger() — the bot's per-trigger view (last row = total).
    Raises on Telegram error so the caller can log it properly.
    """
    lines = [f"📊 *Daily P&L Report*\n_{window_label}_\n"]
    if exchange is not None:
        lines += format_exchange_section(exchange)
        lines.append("")
    lines += format_strategy_section(strategy_rows or [])
    if strategy_rows:
        lines.append("")

    if not rows:
        lines.append("🤖 *Bot triggers:* no trades executed in this window.")
        await bot.send_message(chat_id=chat_id, text="\n".join(lines)[:4000], parse_mode=ParseMode.MARKDOWN)
        return

    lines.append("🤖 *Bot triggers (bot's own records)*")

    data_rows = [r for r in rows if r["trigger_id"] is not None]
    total_row = next((r for r in rows if r["trigger_id"] is None), None)

    for r in data_rows:
        true_pnl  = r.get("true_pnl_usdt") or 0.0
        avg_pct   = r.get("avg_pnl_pct")
        fee       = r.get("fee_usdt", 0.0) or 0.0
        cash_flow = r.get("net_pnl_usdt") or 0.0

        if r.get("market") == "futures":
            if avg_pct is not None:
                pnl_icon = "🟢" if true_pnl >= 0 else "🔴"
                pnl_line = (f"  Realised: {pnl_icon} *${true_pnl:+.4f}* "
                            f"({avg_pct:+.2f}% on margin, {r['wins']}W/{r['losses']}L)")
            else:
                pnl_line = "  Realised: — (no positions closed yet)"
            open_note = f"\n  Still open: {r['open_now']}" if r.get("open_now") else ""
            lines.append(
                f"*#{r['trigger_id']}* `{r['symbol']}` `{r['interval']}` · Futures {r.get('leverage') or 1}×\n"
                f"  {r['buy_count']} opened · {r['sell_count']} closed  Fees: -${fee:.4f}\n"
                f"{pnl_line}{open_note}"
            )
            continue

        # Primary: realised P&L from completed sell trades
        if avg_pct is not None:
            pnl_icon = "🟢" if true_pnl >= 0 else "🔴"
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
        spot_pnl = total_row.get("spot_pnl_usdt") or 0.0
        fut_pnl  = total_row.get("futures_pnl_usdt") or 0.0
        has_fut  = any(r.get("market") == "futures" for r in data_rows)
        split    = f"\n  Spot: ${spot_pnl:+.4f} · Futures: ${fut_pnl:+.4f}" if has_fut else ""
        lines.append(
            f"{'─' * 22}\n"
            f"📋 *TOTAL*  {total_row['buy_count']} entries · {total_row['sell_count']} exits  "
            f"Fees: -${total_fee:.4f}\n"
            f"  Realised P&L: {true_icon} *${total_true:+.4f}*{split}\n"
            f"  Cash flow: {cf_icon} ${total_cf:+.4f}"
        )

    if exchange is not None and total_row:
        ex_fut = (exchange.get("futures_usdt") or {}).get("total", {}).get("net")
        bot_fut = total_row.get("futures_pnl_usdt") or 0.0
        if ex_fut is not None and abs(ex_fut - bot_fut) > 0.10:
            lines.append(f"⚠️ Bot records differ from the exchange by ${ex_fut - bot_fut:+.4f} on futures — "
                         f"exchange SL/TP fills, manual or untracked trades. Trust the exchange figure.")

    lines.append("\n_Realised = per-sell P&L · Cash flow = sells received − buys paid_")
    text = "\n".join(lines)
    await bot.send_message(chat_id=chat_id, text=text[:4000], parse_mode=ParseMode.MARKDOWN)


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


async def send_trade_skipped(bot: Bot, chat_id: str, trigger_id: int, symbol: str, reason: str) -> None:
    """A signal deliberately not traded (filter/guard) — not an error."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⏭ *Signal skipped* — Trigger `#{trigger_id}` `{symbol.upper()}`\nReason: {reason}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


_LEVEL_NAMES = {
    "asia_low": "Asia low", "asia_high": "Asia high", "london_low": "London low",
    "london_high": "London high", "premarket_low": "Pre-market low",
    "premarket_high": "Pre-market high", "pdl": "Prev-day low", "pdh": "Prev-day high",
}


def _lvl(name: str) -> str:
    return _LEVEL_NAMES.get(name, name.replace("_", " "))


async def send_ict_plan(bot: Bot, chat_id: str, trigger_id: int, symbol: str, reference: str,
                        info: dict, mode: str) -> None:
    """09:30 New York session plan: bias + untaken liquidity (the routine's prep step)."""
    bias = info.get("bias", 0)
    bias_txt = {1: "🟢 BULLISH", -1: "🔴 BEARISH"}.get(bias, "⚪ NONE")
    htf = ", ".join(f"{m // 60}h={'+' if v > 0 else '−' if v < 0 else '0'}" for m, v in info.get("htf", {}).items())
    lv = info.get("levels", {}).get("p", {})
    want = ("asia_low", "london_low", "premarket_low", "pdl") if bias > 0 else \
           ("asia_high", "london_high", "premarket_high", "pdh")
    sweep = [f"{_lvl(k)} {lv[k]['price']:,.2f}" for k in want if k in lv and lv[k]["untaken"]]
    lines = [f"🗽 *NY session plan* — Trigger #{trigger_id} `{symbol}` (SMT vs `{reference}`) "
             f"[{mode.upper()}]",
             f"Bias: *{bias_txt}* ({htf})"]
    if bias == 0:
        lines.append(info.get("reason", "No trade today."))
    else:
        lines.append("Watching for a sweep of: " + (" · ".join(sweep) if sweep else "—"))
        lines.append("Then: SMT divergence → displacement FVG → limit at the gap, 09:30–11:00 NY")
    try:
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def send_ict_setup(bot: Bot, chat_id: str, trigger_id: int, symbol: str, setup, qty: float,
                         risk_usdt: float, mode: str) -> None:
    icon = "🟢" if setup.direction == "long" else "🔴"
    text = (
        f"🎯 {icon} *ICT setup {setup.direction.upper()}* `{symbol}` — Trigger #{trigger_id} [{mode.upper()}]\n"
        f"Limit entry: `{setup.entry:,.2f}` (FVG {setup.fvg_low:,.2f}–{setup.fvg_high:,.2f})\n"
        f"SL: `{setup.sl:,.2f}` ({setup.risk_pct:.2f}%) · TP: `{setup.tp:,.2f}` ({setup.rr:.1f}R, "
        f"{setup.tp_source.replace('_', ' ')})\n"
        f"Qty: `{qty:g}` · risk ≈ ${risk_usdt:.2f}\n"
        f"SMT: {setup.smt_swept_by} swept {_lvl(setup.smt_level)}\n"
        "Order expires in 30 min if price does not retrace."
    )
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass


async def send_ict_update(bot: Bot, chat_id: str, trigger_id: int, symbol: str, event: str,
                          mode: str, detail: str = "") -> None:
    icons = {"filled": "✅", "cancelled": "🚫", "closed": "🏁", "error": "⚠️"}
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{icons.get(event, '•')} *ICT {event}* `{symbol}` — Trigger #{trigger_id} "
                 f"[{mode.upper()}]\n{detail}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass


async def send_unprotected_position_alert(bot: Bot, chat_id: str, pos: dict) -> None:
    """A live futures position with no exchange stop-loss — the pattern behind the biggest losses."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(f"🚨 *Futures position WITHOUT stop-loss*\n"
                  f"`{_pair_label(pos['pair'])}` {pos['side'].upper()} {pos['qty']:g} @ {pos['avg']:,.2f} "
                  f"(mark {pos['mark']:,.2f}, uP&L {pos['upnl']:+.4f}, liq {pos['liq']:,.2f})\n"
                  "Nothing protects this position if price runs. Add a stop in the app, or close it."),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
