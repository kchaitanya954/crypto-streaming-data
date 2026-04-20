"""
Auto-trade execution for matched triggers.

Called by the background signal worker (orchestrator.py) — runs 24/7
regardless of whether a browser tab is open.

Applies adaptive position sizing via the AdaptiveEngine before placing orders.

Improvements:
  - Telegram trade confirmation sent immediately after execution
  - Max concurrent open positions guard (prevents over-trading)
  - Stop-loss / take-profit prices stored on every BUY for stream monitoring
"""

import asyncio
import logging
import math

_log = logging.getLogger("auto_trade")
_engine_lock = asyncio.Lock()

# Maximum number of open positions per user before new BUYs are blocked.
MAX_OPEN_POSITIONS = 5

# Default risk parameters (% of avg entry price)
STOP_LOSS_PCT    = 2.0   # sell if price drops 2% below avg entry
TAKE_PROFIT_PCT  = 4.0   # sell if price rises 4% above avg entry

# CoinDCX taker fee (market orders). Round-trip cost = 2x this.
# TP must exceed 2x fee to be profitable; SL is tightened by the sell-side fee.
COINDCX_FEE_RATE = 0.001  # 0.1%

# Market constraints cache.
# Keys: base currency ("BTC", "ETH"); values: {"min_qty": float, "step": float}
# Seeded with known-correct values from real CoinDCX error messages.
# Updated at runtime when OMS-VF-0004 reveals the actual minimum.
_market_cache: dict[str, dict] = {
    "BTC":   {"min_qty": 0.00001, "step": 0.00001},
    "ETH":   {"min_qty": 0.0001,  "step": 0.0001},
    "BNB":   {"min_qty": 0.001,   "step": 0.001},
    "SOL":   {"min_qty": 0.01,    "step": 0.01},
    "XRP":   {"min_qty": 1.0,     "step": 1.0},
    "DOGE":  {"min_qty": 1.0,     "step": 1.0},
    "MATIC": {"min_qty": 1.0,     "step": 1.0},
    "ADA":   {"min_qty": 1.0,     "step": 1.0},
    "AVAX":  {"min_qty": 0.01,    "step": 0.01},
    "LINK":  {"min_qty": 0.1,     "step": 0.1},
    "DOT":   {"min_qty": 0.1,     "step": 0.1},
    "LTC":   {"min_qty": 0.001,   "step": 0.001},
}
_DEFAULT_CONSTRAINTS = {"min_qty": 0.0001, "step": 0.0001}


def _get_market_constraints(base: str) -> dict:
    """Return {min_qty, step} for a base currency. Falls back to safe defaults."""
    return _market_cache.get(base.upper(), _DEFAULT_CONSTRAINTS)


def _learn_min_qty_from_error(base: str, error_msg: str) -> None:
    """
    Parse the actual minimum from a CoinDCX OMS-VF-0004 error message and cache it.
    Message format: "Quantity should be greater than 0.0001"
    """
    import re
    m = re.search(r"greater than\s+([\d.]+)", error_msg)
    if m:
        try:
            actual_min = float(m.group(1))
            _market_cache[base.upper()] = {"min_qty": actual_min, "step": actual_min}
            _log.info("Learned min_qty for %s = %s (from exchange error)", base.upper(), actual_min)
        except ValueError:
            pass


def _apply_constraints(raw: float, constraints: dict) -> float:
    """Floor quantity to the market's step size."""
    step   = constraints["step"]
    factor = 1.0 / step
    return math.floor(raw * factor) / factor


async def _get_user_telegram(user_settings: dict):
    """Return (Bot, chat_id) from user settings, or (None, None) if not configured."""
    from auth.encryption import safe_decrypt
    from telegram import Bot

    tg_token   = safe_decrypt((user_settings or {}).get("telegram_token"))
    tg_chat_id = safe_decrypt((user_settings or {}).get("telegram_chat_id"))
    if tg_token and tg_chat_id:
        return Bot(token=tg_token), tg_chat_id
    return None, None


async def execute_trigger_trade(
    db,
    trigger: dict,
    signal,
    symbol: str,
    interval: str,
    signal_id,
    reason: str = "signal",
) -> None:
    """
    Execute a market order on CoinDCX for a matched trigger.

    BUY  → spend adaptive % of trigger.trade_amount_usdt to buy base asset.
    SELL → sell adaptive fraction of coins held by this trigger's position.

    Updates trigger_positions and feeds the adaptive engine with real P&L.

    reason: "signal" | "stop_loss" | "take_profit" — included in notifications.
    """
    import aiohttp
    from auth.encryption import safe_decrypt
    from database import queries
    from exchange.coindcx_client import CoinDCXClient
    from signals.adaptive_strategy import engine as _adaptive_engine
    from telegram_bot.alerts import send_trade_notification, send_trade_error

    user_id    = trigger.get("user_id")
    trigger_id = trigger["id"]
    amount     = trigger.get("trade_amount_usdt") or 0.0

    if not user_id or not amount:
        return  # legacy trigger without user or amount — skip

    user_settings = await queries.get_user_settings(db, user_id)
    if not user_settings:
        _log.warning("Trigger %d: no settings for user %d — skipping trade", trigger_id, user_id)
        return

    key    = safe_decrypt(user_settings.get("coindcx_api_key_enc"))
    secret = safe_decrypt(user_settings.get("coindcx_api_secret_enc"))
    if not key or not secret:
        _log.warning("Trigger %d: user %d has no CoinDCX API keys — skipping trade.", trigger_id, user_id)
        return

    tg_bot, tg_chat_id = await _get_user_telegram(user_settings)

    try:
        async with aiohttp.ClientSession() as session:
            exchange = CoinDCXClient(api_key=key, api_secret=secret, session=session)

            # ── BUY ──────────────────────────────────────────────────────────
            if signal.direction == "BUY":
                losses = _adaptive_engine.consecutive_losses
                dd     = _adaptive_engine.current_drawdown_pct

                # Adaptive engine already handles drawdown via Kelly reduction
                # and drawdown multipliers (30% of normal size at DD≥20%).
                # Blocking trades entirely prevents recovery — size reduction is
                # always better than a hard stop.
                if losses > 0 or dd > 0:
                    _log.info(
                        "Trigger %d: DD=%.1f%%  losses=%d — Kelly-reduced sizing (adaptive)",
                        trigger_id, dd, losses,
                    )

                # ── Max concurrent open positions check ──────────────────────
                open_count = await queries.get_open_positions_count(db, user_id)
                if open_count >= MAX_OPEN_POSITIONS:
                    _log.warning(
                        "Trigger %d: user %d already has %d open positions (max %d) — skipping BUY",
                        trigger_id, user_id, open_count, MAX_OPEN_POSITIONS,
                    )
                    if tg_bot:
                        await send_trade_error(
                            tg_bot, tg_chat_id, trigger_id, symbol, "BUY",
                            f"Max {MAX_OPEN_POSITIONS} open positions reached — wait for a SELL",
                        )
                    return

                # ── Adaptive slice sizing (% of *remaining* trigger budget) ──
                # BUY deploys a fraction of what's left unspent in this trigger.
                # Example: trigger=$10, MEDIUM → 50% of $10=$5 first trade,
                #          then 50% of remaining $5=$2.50, then $1.25, etc.
                # This prevents over-investing and keeps capital for DCA signals.
                pos_before   = await queries.get_trigger_position(db, trigger_id)
                usdt_spent   = pos_before["usdt_spent"] if pos_before else 0.0
                remaining    = max(round(amount - usdt_spent, 4), 0.0)

                if remaining < 0.50:
                    _log.info(
                        "Trigger %d: remaining budget $%.4f exhausted — skipping BUY",
                        trigger_id, remaining,
                    )
                    return

                adaptive_pct  = _adaptive_engine.buy_pct_for(signal.confidence, signal.adx_val)
                adaptive_usdt = round(remaining * adaptive_pct / 100.0, 4)
                # Clamp to remaining budget (never exceed what's left)
                adaptive_usdt = min(adaptive_usdt, remaining)

                _log.info("Trigger %d BUY sizing: budget=$%.2f  adaptive=%.1f%%  order=$%.2f",
                          trigger_id, amount, adaptive_pct, adaptive_usdt)

                balances = await exchange.get_balances()
                usdt_row = next((b for b in balances if b.get("currency") == "USDT"), None)
                usdt_bal = float(usdt_row.get("balance", 0)) if usdt_row else 0.0
                if usdt_bal < adaptive_usdt:
                    _log.warning(
                        "Trigger %d: insufficient USDT %.2f < %.2f — skipping BUY",
                        trigger_id, usdt_bal, adaptive_usdt,
                    )
                    if tg_bot:
                        await send_trade_error(
                            tg_bot, tg_chat_id, trigger_id, symbol, "BUY",
                            f"Insufficient USDT: ${usdt_bal:.2f} available, ${adaptive_usdt:.2f} needed",
                        )
                    return

                base_currency = symbol.upper().replace("USDT", "")

                # Quantity floored to known step size (precision only — no pre-validation)
                constraints = _get_market_constraints(base_currency)
                qty_gross   = adaptive_usdt / signal.entry_price
                qty         = _apply_constraints(qty_gross * (1 - COINDCX_FEE_RATE), constraints)

                from aiohttp import ClientResponseError as _CRE
                try:
                    result = await exchange.create_order(
                        side="buy", market=symbol,
                        order_type="market_order", quantity=qty,
                    )
                except _CRE as exc:
                    msg = str(exc.message)
                    if exc.status == 400 and any(
                        c in msg for c in ("OMS-VF-0004", "OMS-VF-0006")
                    ):
                        _learn_min_qty_from_error(base_currency, msg)
                        real_min  = _get_market_constraints(base_currency)["min_qty"]
                        min_usdt  = round(real_min * signal.entry_price, 4)
                        warn_text = (
                            f"BUY slice ${adaptive_usdt:.4f} is below the exchange minimum "
                            f"({real_min} {base_currency} ≈ ${min_usdt:.2f}). "
                            f"Increase trigger amount or confidence threshold."
                        )
                        _log.warning("Trigger %d: %s", trigger_id, warn_text)
                        if tg_bot:
                            await send_trade_error(tg_bot, tg_chat_id, trigger_id, symbol, "BUY", warn_text)
                        return
                    raise
                cdx_order_id = result.get("id") or result.get("order_id")

                order_id = await queries.insert_order(
                    db, symbol, "buy", "market_order", qty,
                    price=signal.entry_price, signal_id=signal_id,
                    user_id=user_id, trigger_id=trigger_id,
                )
                await queries.update_order_status(db, order_id, "filled", cdx_order_id)

                # DCA blend: if a position already exists, blend avg entry
                pos = await queries.get_trigger_position(db, trigger_id)
                # Effective entry price accounting for BUY fee (break-even is higher)
                effective_entry = signal.entry_price * (1 + COINDCX_FEE_RATE)
                if pos and pos["coins_held"] > 0:
                    total_coins = pos["coins_held"] + qty
                    new_avg = (
                        pos["avg_entry"] * pos["coins_held"] + effective_entry * qty
                    ) / total_coins
                    # SL/TP account for SELL-side fee too (round-trip cost = 2x fee)
                    new_sl = round(new_avg * (1 - STOP_LOSS_PCT / 100), 8)
                    new_tp = round(new_avg * (1 + TAKE_PROFIT_PCT / 100 + COINDCX_FEE_RATE), 8)
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, total_coins, new_avg,
                        pos["usdt_spent"] + adaptive_usdt,
                        stop_loss_price=new_sl, take_profit_price=new_tp,
                    )
                else:
                    # TP must exceed round-trip fee (2x) to be profitable
                    sl_price = round(effective_entry * (1 - STOP_LOSS_PCT / 100), 8)
                    tp_price = round(effective_entry * (1 + TAKE_PROFIT_PCT / 100 + COINDCX_FEE_RATE), 8)
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, qty, effective_entry, adaptive_usdt,
                        stop_loss_price=sl_price, take_profit_price=tp_price,
                    )
                    _log.info(
                        "Trigger %d SL=%.4f  TP=%.4f  (fee-adjusted effective_entry=%.4f)",
                        trigger_id, sl_price, tp_price, effective_entry,
                    )

                _log.info(
                    "Trigger %d BUY: %.8f %s @ %.4f  adaptive=%.0f%% → $%.2f",
                    trigger_id, qty, symbol, signal.entry_price, adaptive_pct, adaptive_usdt,
                )

                # ── Telegram confirmation ─────────────────────────────────────
                if tg_bot:
                    await send_trade_notification(
                        tg_bot, tg_chat_id,
                        side="BUY", symbol=symbol,
                        quantity=qty, price=signal.entry_price,
                        usdt_amount=adaptive_usdt,
                        trigger_id=trigger_id, reason=reason,
                    )

            # ── SELL ─────────────────────────────────────────────────────────
            elif signal.direction == "SELL":
                base_currency = symbol.upper().replace("USDT", "")
                pos = await queries.get_trigger_position(db, trigger_id)

                # SELL only operates on coins this trigger bought (tracked in DB).
                # Never touch the user's other holdings — trigger budget is isolated.
                if not pos or pos["coins_held"] < 1e-8:
                    _log.info("Trigger %d SELL skipped: trigger has no open position", trigger_id)
                    return

                # Verify actual exchange balance — clears stale DB records
                actual_bal = pos["coins_held"]  # default: trust DB
                try:
                    balances  = await exchange.get_balances()
                    coin_row  = next(
                        (b for b in balances if b.get("currency") == base_currency), None
                    )
                    actual_bal = float(coin_row.get("balance", 0)) if coin_row else 0.0
                    if actual_bal < 1e-8:
                        _log.warning(
                            "Trigger %d SELL: DB shows %.8f %s but exchange balance=0 — clearing stale position",
                            trigger_id, pos["coins_held"], base_currency,
                        )
                        await queries.upsert_trigger_position(
                            db, trigger_id, symbol, 0.0, 0.0, 0.0
                        )
                        return
                except Exception as bal_exc:
                    _log.warning("Trigger %d: could not verify balance before SELL (%s) — proceeding with DB",
                                 trigger_id, bal_exc)

                # For stop-loss / take-profit, always sell the full position
                if reason in ("stop_loss", "take_profit"):
                    sell_ratio = 1.0
                else:
                    sell_ratio = _adaptive_engine.sell_ratio_for(signal.confidence)

                # Sell from trigger's tracked position, capped to actual exchange balance
                coins_to_sell = min(round(pos["coins_held"] * sell_ratio, 8), actual_bal)
                if coins_to_sell < 1e-8:
                    return

                # Floor both quantities to exchange step size
                constraints   = _get_market_constraints(base_currency)
                full_qty      = _apply_constraints(pos["coins_held"], constraints)
                coins_to_sell = _apply_constraints(coins_to_sell, constraints)

                # Step-floor can produce 0 for dust (e.g. 0.000042 ETH with step 0.0001 → 0)
                if coins_to_sell <= 0:
                    if full_qty > 0:
                        # Partial floored to 0 — escalate to full position
                        _log.info(
                            "Trigger %d: partial SELL floored to 0 — escalating to full %.8f %s",
                            trigger_id, full_qty, base_currency,
                        )
                        coins_to_sell = full_qty
                    else:
                        # Full position also rounds to 0 — it's genuinely dust, clear DB
                        _log.warning(
                            "Trigger %d: position %.8f %s rounds to 0 at step %.8f — clearing DB as dust",
                            trigger_id, pos["coins_held"], base_currency, constraints["step"],
                        )
                        await queries.upsert_trigger_position(db, trigger_id, symbol, 0.0, 0.0, 0.0)
                        return

                from aiohttp import ClientResponseError as _CRE
                async def _place_sell(qty: float) -> dict:
                    return await exchange.create_order(
                        side="sell", market=symbol,
                        order_type="market_order", quantity=qty,
                    )

                try:
                    result = await _place_sell(coins_to_sell)
                except _CRE as exc:
                    msg = str(exc.message)
                    _qty_error = exc.status in (400, 422) and any(
                        c in msg for c in (
                            "OMS-VF-0004", "OMS-VF-0006",
                            "BFF-SO-004",
                            "total_quantity", "positive number",
                            "Quantity should be",
                        )
                    )
                    if _qty_error:
                        # Learn real minimum from the error, then escalate or clear
                        _learn_min_qty_from_error(base_currency, msg)
                        real_min = _get_market_constraints(base_currency)["min_qty"]

                        if full_qty >= real_min and full_qty > coins_to_sell:
                            # Partial was too small — escalate to full position
                            _log.info(
                                "Trigger %d: partial %.8f below min %.8f — escalating to full %.8f %s",
                                trigger_id, coins_to_sell, real_min, full_qty, base_currency,
                            )
                            try:
                                result = await _place_sell(full_qty)
                                coins_to_sell = full_qty
                            except _CRE as exc2:
                                _learn_min_qty_from_error(base_currency, str(exc2.message))
                                _log.warning(
                                    "Trigger %d: full position %.8f %s still rejected — clearing DB as dust",
                                    trigger_id, full_qty, base_currency,
                                )
                                await queries.upsert_trigger_position(db, trigger_id, symbol, 0.0, 0.0, 0.0)
                                return
                        else:
                            # Full position is also below minimum — it's dust
                            dust_usdt = round(full_qty * signal.entry_price, 4)
                            _log.warning(
                                "Trigger %d: position %.8f %s ($%.4f) is below exchange min %.8f — clearing DB",
                                trigger_id, full_qty, base_currency, dust_usdt, real_min,
                            )
                            await queries.upsert_trigger_position(db, trigger_id, symbol, 0.0, 0.0, 0.0)
                            return
                    else:
                        raise
                cdx_order_id = result.get("id") or result.get("order_id")
                avg_entry    = (pos["avg_entry"] if pos else None) or signal.entry_price
                # Net proceeds after SELL-side fee; P&L vs fee-adjusted avg_entry
                net_sell_price = signal.entry_price * (1 - COINDCX_FEE_RATE)
                pnl_pct        = (net_sell_price - avg_entry) / avg_entry * 100
                sell_usdt      = round(coins_to_sell * net_sell_price, 4)

                order_id = await queries.insert_order(
                    db, symbol, "sell", "market_order", coins_to_sell,
                    price=signal.entry_price, signal_id=signal_id,
                    user_id=user_id, trigger_id=trigger_id,
                )
                await queries.update_order_status(db, order_id, "filled", cdx_order_id)
                await queries.insert_trade(
                    db, order_id, symbol, "sell",
                    coins_to_sell, signal.entry_price, pnl=pnl_pct,
                )

                # Reduce position in DB
                db_held      = pos["coins_held"] if pos else 0.0
                db_spent     = pos["usdt_spent"] if pos else 0.0
                coins_remaining = max(round(db_held - coins_to_sell, 8), 0.0)
                usdt_remaining  = max(round(db_spent * (1.0 - sell_ratio), 4), 0.0)
                await queries.upsert_trigger_position(
                    db, trigger_id, symbol,
                    coins_remaining, avg_entry, usdt_remaining,
                )

                # ── Update adaptive engine with full real trade history ────────
                # Read ALL completed real trades (not just this one) so the
                # rolling window, win-rate, and drawdown are always accurate.
                all_pnls = await queries.get_real_completed_pnls(db)
                if all_pnls:
                    running = 100.0
                    enriched = []
                    for t in all_pnls:
                        running = running * (1 + t["pnl_pct"] / 100)
                        enriched.append({"pnl_pct": t["pnl_pct"], "portfolio_val": running})
                    async with _engine_lock:
                        _adaptive_engine.update(enriched)
                await queries.save_adaptive_state(db, _adaptive_engine.to_dict())

                _log.info(
                    "Trigger %d SELL(%s): %.8f %s @ %.4f  ratio=%.0f%%  PnL=%.4f%%  remaining=%.8f",
                    trigger_id, reason, coins_to_sell, symbol, signal.entry_price,
                    sell_ratio * 100, pnl_pct, coins_remaining,
                )

                # ── Telegram confirmation ─────────────────────────────────────
                if tg_bot:
                    await send_trade_notification(
                        tg_bot, tg_chat_id,
                        side="SELL", symbol=symbol,
                        quantity=coins_to_sell, price=signal.entry_price,
                        usdt_amount=sell_usdt,
                        trigger_id=trigger_id, pnl_pct=pnl_pct, reason=reason,
                    )

    except Exception as exc:
        import traceback
        _log.error("Auto-trade failed for trigger %d: %s\n%s",
                   trigger_id, exc, traceback.format_exc())
        if tg_bot:
            await send_trade_error(
                tg_bot, tg_chat_id, trigger_id, symbol,
                signal.direction,
                str(exc)[:200],
            )
