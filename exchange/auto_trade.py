"""
Auto-trade execution for matched triggers.

Called by the background signal worker (orchestrator.py) — runs 24/7
regardless of whether a browser tab is open.

Applies adaptive position sizing via the AdaptiveEngine before placing orders.
"""

import asyncio
import logging

_log = logging.getLogger("auto_trade")
_engine_lock = asyncio.Lock()


async def execute_trigger_trade(
    db,
    trigger: dict,
    signal,
    symbol: str,
    interval: str,
    signal_id,
) -> None:
    """
    Execute a market order on CoinDCX for a matched trigger.

    BUY  → spend adaptive % of trigger.trade_amount_usdt to buy base asset.
    SELL → sell adaptive fraction of coins held by this trigger's position.

    Updates trigger_positions and feeds the adaptive engine with real P&L.
    """
    import aiohttp
    from auth.encryption import safe_decrypt
    from database import queries
    from exchange.coindcx_client import CoinDCXClient
    from signals.adaptive_strategy import engine as _adaptive_engine

    user_id    = trigger.get("user_id")
    trigger_id = trigger["id"]
    amount     = trigger.get("trade_amount_usdt") or 0.0

    if not user_id or not amount:
        return  # legacy trigger without user or amount — skip

    user_settings = await queries.get_user_settings(db, user_id)
    if not user_settings:
        return

    key    = safe_decrypt(user_settings.get("coindcx_api_key_enc"))
    secret = safe_decrypt(user_settings.get("coindcx_api_secret_enc"))
    if not key or not secret:
        return

    try:
        async with aiohttp.ClientSession() as session:
            exchange = CoinDCXClient(api_key=key, api_secret=secret, session=session)

            # ── BUY ──────────────────────────────────────────────────────────
            if signal.direction == "BUY":
                if _adaptive_engine.circuit_breaker:
                    _log.warning("Trigger %d: circuit breaker active — skipping BUY", trigger_id)
                    return

                # Scale the trigger budget by the adaptive engine's recommended %
                adaptive_pct  = _adaptive_engine.buy_pct_for(signal.confidence, signal.adx_val)
                adaptive_usdt = max(round(amount * adaptive_pct / 100.0, 4), 1.0)

                balances = await exchange.get_balances()
                usdt_row = next((b for b in balances if b.get("currency") == "USDT"), None)
                usdt_bal = float(usdt_row.get("balance", 0)) if usdt_row else 0.0
                if usdt_bal < adaptive_usdt:
                    _log.warning(
                        "Trigger %d: insufficient USDT %.2f < %.2f (adaptive %.0f%% of $%.2f)",
                        trigger_id, usdt_bal, adaptive_usdt, adaptive_pct, amount,
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
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, total_coins, new_avg,
                        pos["usdt_spent"] + adaptive_usdt,
                    )
                else:
                    await queries.upsert_trigger_position(
                        db, trigger_id, symbol, qty, signal.entry_price, adaptive_usdt,
                    )

                _log.info(
                    "Trigger %d BUY: %.8f %s @ %.4f  adaptive=%.0f%% → $%.2f",
                    trigger_id, qty, symbol, signal.entry_price, adaptive_pct, adaptive_usdt,
                )

            # ── SELL ─────────────────────────────────────────────────────────
            elif signal.direction == "SELL":
                pos = await queries.get_trigger_position(db, trigger_id)
                if not pos or pos["coins_held"] < 1e-8:
                    return  # nothing to sell

                sell_ratio    = _adaptive_engine.sell_ratio_for(signal.confidence)
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

                # Portfolio value for adaptive engine update
                try:
                    balances_after = await exchange.get_balances()
                    portfolio_val = sum(
                        float(b.get("balance", 0))
                        for b in balances_after
                        if b.get("currency") == "USDT"
                    )
                except Exception:
                    portfolio_val = _adaptive_engine.peak_portfolio

                async with _engine_lock:
                    _adaptive_engine.update([{"pnl_pct": pnl_pct, "portfolio_val": portfolio_val}])

                from database import queries as _q
                await _q.save_adaptive_state(db, _adaptive_engine.to_dict())

                _log.info(
                    "Trigger %d SELL: %.8f %s @ %.4f  ratio=%.0f%%  PnL=%.4f%%  remaining=%.8f",
                    trigger_id, coins_to_sell, symbol, signal.entry_price,
                    sell_ratio * 100, pnl_pct, coins_remaining,
                )

    except Exception as exc:
        _log.error("Auto-trade failed for trigger %d: %s", trigger_id, exc)
