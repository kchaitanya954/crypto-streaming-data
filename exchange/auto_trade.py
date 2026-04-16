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

_log = logging.getLogger("auto_trade")
_engine_lock = asyncio.Lock()

# Maximum number of open positions per user before new BUYs are blocked.
MAX_OPEN_POSITIONS = 5

# Default risk parameters (% of avg entry price)
STOP_LOSS_PCT    = 2.0   # sell if price drops 2% below avg entry
TAKE_PROFIT_PCT  = 4.0   # sell if price rises 4% above avg entry


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

                # ── Extreme drawdown: only hard stop (genuine emergency) ──────
                if dd >= 20.0:
                    _log.warning("Trigger %d: HARD STOP — portfolio down %.1f%% — skipping BUY",
                                 trigger_id, dd)
                    if tg_bot:
                        await send_trade_error(
                            tg_bot, tg_chat_id, trigger_id, symbol, "BUY",
                            f"Hard stop: portfolio drawdown {dd:.1f}% (>20%) — resume manually after review",
                        )
                    return

                # ── Adaptive signal filter (tighten criteria, keep trading) ───
                # After real consecutive losses, we don't STOP — we get SELECTIVE.
                # Adaptive position sizing already reduces size; this layer
                # additionally demands stronger signal quality.
                if losses >= 5:
                    # 5+ real losses: only act on HIGH confidence with strong ADX
                    if signal.confidence != "HIGH":
                        _log.info("Trigger %d: adaptive filter (5+ losses) — skipping %s confidence BUY, need HIGH",
                                  trigger_id, signal.confidence)
                        return
                    if (signal.adx_val or 0) < 30:
                        _log.info("Trigger %d: adaptive filter (5+ losses) — skipping ADX=%.1f BUY, need ≥30",
                                  trigger_id, signal.adx_val or 0)
                        return
                    _log.info("Trigger %d: adaptive filter PASSED (5+ losses, HIGH+ADX≥30)", trigger_id)

                elif losses >= 3:
                    # 3-4 real losses: raise bar to HIGH confidence only
                    if signal.confidence not in ("HIGH", "MEDIUM"):
                        _log.info("Trigger %d: adaptive filter (3+ losses) — skipping LOW confidence BUY",
                                  trigger_id)
                        return
                    if signal.confidence == "MEDIUM" and (signal.adx_val or 0) < 25:
                        _log.info("Trigger %d: adaptive filter (3+ losses) — MEDIUM needs ADX≥25, got %.1f",
                                  trigger_id, signal.adx_val or 0)
                        return
                    _log.info("Trigger %d: adaptive filter PASSED (3+ losses, %s confidence)", trigger_id, signal.confidence)

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

                # Scale the trigger budget by the adaptive engine's recommended %
                adaptive_pct  = _adaptive_engine.buy_pct_for(signal.confidence, signal.adx_val)
                adaptive_usdt = max(round(amount * adaptive_pct / 100.0, 4), 5.0)  # $5 min (CoinDCX floor)
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

                qty = round(adaptive_usdt / signal.entry_price, 8)
                result = await exchange.create_order(
                    side="buy", market=symbol,
                    order_type="market_order", quantity=qty,
                )
                cdx_order_id = result.get("id") or result.get("order_id")

                order_id = await queries.insert_order(
                    db, symbol, "buy", "market_order", qty,
                    price=signal.entry_price, signal_id=signal_id,
                    user_id=user_id, trigger_id=trigger_id,
                )
                await queries.update_order_status(db, order_id, "filled", cdx_order_id)

                # DCA blend: if a position already exists, blend avg entry
                pos = await queries.get_trigger_position(db, trigger_id)
                if pos and pos["coins_held"] > 0:
                    total_coins = pos["coins_held"] + qty
                    new_avg = (
                        pos["avg_entry"] * pos["coins_held"] + signal.entry_price * qty
                    ) / total_coins
                    new_sl = round(new_avg * (1 - STOP_LOSS_PCT / 100), 8)
                    new_tp = round(new_avg * (1 + TAKE_PROFIT_PCT / 100), 8)
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, total_coins, new_avg,
                        pos["usdt_spent"] + adaptive_usdt,
                        stop_loss_price=new_sl, take_profit_price=new_tp,
                    )
                else:
                    sl_price = round(signal.entry_price * (1 - STOP_LOSS_PCT / 100), 8)
                    tp_price = round(signal.entry_price * (1 + TAKE_PROFIT_PCT / 100), 8)
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, qty, signal.entry_price, adaptive_usdt,
                        stop_loss_price=sl_price, take_profit_price=tp_price,
                    )
                    _log.info(
                        "Trigger %d SL=%.4f  TP=%.4f",
                        trigger_id, sl_price, tp_price,
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
                pos = await queries.get_trigger_position(db, trigger_id)
                if not pos or pos["coins_held"] < 1e-8:
                    return  # nothing to sell

                # For stop-loss / take-profit, always sell the full position
                if reason in ("stop_loss", "take_profit"):
                    sell_ratio = 1.0
                else:
                    sell_ratio = _adaptive_engine.sell_ratio_for(signal.confidence)

                coins_to_sell = round(pos["coins_held"] * sell_ratio, 8)
                if coins_to_sell < 1e-8:
                    return

                result = await exchange.create_order(
                    side="sell", market=symbol,
                    order_type="market_order", quantity=coins_to_sell,
                )
                cdx_order_id = result.get("id") or result.get("order_id")
                avg_entry    = pos["avg_entry"]
                pnl_pct      = (signal.entry_price - avg_entry) / avg_entry * 100 if avg_entry else 0.0
                sell_usdt    = round(coins_to_sell * signal.entry_price, 4)

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

                # Reduce position (partial sell support)
                coins_remaining = max(round(pos["coins_held"] - coins_to_sell, 8), 0.0)
                usdt_remaining  = max(round(pos["usdt_spent"] * (1.0 - sell_ratio), 4), 0.0)
                await queries.upsert_trigger_position(
                    db, trigger_id, symbol,
                    coins_remaining, pos["avg_entry"], usdt_remaining,
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
