"""
Futures trade executor.

Signal mapping:
  BUY  signal → open LONG  position (or close existing SHORT first)
  SELL signal → open SHORT position (or close existing LONG first)

Position sizing:
  margin_usdt = adaptive % of trigger's remaining budget
  notional    = margin_usdt × leverage
  quantity    = notional / entry_price

SL/TP algorithm (ATR-based, leverage-aware):
  1. Compute ATR(14) from recent candles in DB
  2. sl_pct = max(1.5 × atr_pct, MIN_SL_PCT) — price % distance from entry
  3. Clamp sl_pct so margin loss never exceeds MAX_SL_MARGIN_LOSS (15%)
  4. tp_pct = max(sl_pct × 2.5, MIN_TP_MARGIN_GAIN / leverage) — min 2.5:1 RR
  5. Clamp SL to stay at least 30% away from liquidation (safety buffer)
  6. For LONG: sl below entry, tp above. For SHORT: sl above, tp below.

Funding rate filter (soft):
  If funding rate > 0.10% → reduce LONG sizing by 20% (market overleveraged long)
  If funding rate < -0.10% → reduce SHORT sizing by 20% (market overleveraged short)
"""

import asyncio
import logging
import math

_log = logging.getLogger("futures_trade")

FUTURES_FEE_RATE  = 0.0005   # 0.05% per side (taker, futures)
MAX_SL_MARGIN_LOSS = 0.15    # 15% margin loss triggers SL
MIN_TP_MARGIN_GAIN = 0.25    # 25% margin gain triggers TP
MIN_SL_PCT        = 0.003    # never tighter than 0.3% price move
LIQ_BUFFER_RATIO  = 0.70     # SL must stay within 70% of liq distance from entry
FUNDING_SKEW_PCT  = 0.001    # 0.1% funding rate → apply sizing skew

# Maintenance margin rate (approximate; varies by exchange/pair)
MAINTENANCE_MARGIN = 0.005   # 0.5%


def compute_liquidation_price(side: str, entry_price: float, leverage: int) -> float:
    """
    Approximate liquidation price.
    LONG:  liq = entry × (1 - (1 - MM) / leverage)
    SHORT: liq = entry × (1 + (1 - MM) / leverage)
    """
    factor = (1.0 - MAINTENANCE_MARGIN) / leverage
    if side == "long":
        return round(entry_price * (1.0 - factor), 8)
    else:
        return round(entry_price * (1.0 + factor), 8)


def compute_futures_sl_tp(
    side: str,
    entry_price: float,
    leverage: int,
    atr_pct: float,           # ATR as % of entry_price (e.g. 0.8 means 0.8%)
) -> tuple[float, float, float]:
    """
    Returns (sl_price, tp_price, liquidation_price).

    SL algorithm:
      sl_pct = max(1.5 × atr_pct, MIN_SL_PCT)   — raw ATR-based distance
      sl_pct = min(sl_pct, MAX_SL_MARGIN_LOSS / leverage)  — margin-loss cap
      sl_pct = min(sl_pct, LIQ_BUFFER_RATIO / leverage)    — liquidation buffer
    TP algorithm:
      tp_pct = max(sl_pct × 2.5, MIN_TP_MARGIN_GAIN / leverage)   — min 2.5:1 RR
    """
    # Step 1: ATR-based SL distance (% of entry price)
    sl_pct_raw = max(1.5 * atr_pct / 100.0, MIN_SL_PCT)

    # Step 2: Cap so margin loss ≤ MAX_SL_MARGIN_LOSS
    sl_pct_margin_cap = MAX_SL_MARGIN_LOSS / leverage
    sl_pct = min(sl_pct_raw, sl_pct_margin_cap)

    # Step 3: Ensure SL stays LIQ_BUFFER_RATIO away from liquidation
    liq_distance_pct = (1.0 - MAINTENANCE_MARGIN) / leverage
    sl_pct = min(sl_pct, liq_distance_pct * LIQ_BUFFER_RATIO)

    # Step 4: TP — minimum 2.5:1 risk-reward, minimum margin gain
    min_tp_pct = MIN_TP_MARGIN_GAIN / leverage
    tp_pct = max(sl_pct * 2.5, min_tp_pct)

    liq_price = compute_liquidation_price(side, entry_price, leverage)

    if side == "long":
        sl_price = round(entry_price * (1.0 - sl_pct), 8)
        tp_price = round(entry_price * (1.0 + tp_pct), 8)
    else:  # short
        sl_price = round(entry_price * (1.0 + sl_pct), 8)
        tp_price = round(entry_price * (1.0 - tp_pct), 8)

    return sl_price, tp_price, liq_price


async def _get_atr_pct(db, symbol: str, interval: str, entry_price: float) -> float:
    """
    Compute ATR(14) from DB candles and return it as % of entry_price.
    Falls back to 0.8% if insufficient data.
    """
    from database import queries
    from indicators.indicators import atr as calc_atr

    DEFAULT_ATR_PCT = 0.8  # conservative fallback
    try:
        candles = await queries.get_candles_for_atr(db, symbol, interval, limit=30)
        if len(candles) < 15:
            return DEFAULT_ATR_PCT
        highs  = [c["high"]  for c in candles]
        lows   = [c["low"]   for c in candles]
        closes = [c["close"] for c in candles]
        atr_series = calc_atr(highs, lows, closes, period=14)
        # Get last valid ATR value
        atr_val = next((v for v in reversed(atr_series) if v is not None), None)
        if atr_val is None or entry_price <= 0:
            return DEFAULT_ATR_PCT
        return round(atr_val / entry_price * 100, 4)
    except Exception as exc:
        _log.warning("ATR computation failed (%s) — using default %.1f%%", exc, DEFAULT_ATR_PCT)
        return DEFAULT_ATR_PCT


async def execute_futures_trigger_trade(
    db,
    trigger: dict,
    signal,
    symbol: str,
    interval: str,
    signal_id=None,
    reason: str = "signal",
) -> None:
    """
    Main futures trade executor. Called by orchestrator for futures triggers.

    Flow:
      1. Determine position side (LONG for BUY, SHORT for SELL)
      2. Check for existing open position on this trigger
      3. If opposite position open → close it first
      4. If same-side position already open → skip (already positioned)
      5. Compute margin from adaptive sizing
      6. Compute ATR-based SL/TP
      7. Place market order on CoinDCX futures
      8. Record in futures_positions table
      9. Send Telegram confirmation
    """
    import aiohttp
    from auth.encryption import safe_decrypt
    from database import queries
    from exchange.coindcx_client import CoinDCXClient
    from signals.adaptive_strategy import engine as _adaptive_engine
    from telegram_bot.alerts import send_futures_trade_notification
    import asyncio

    user_id    = trigger.get("user_id")
    trigger_id = trigger["id"]
    leverage   = int(trigger.get("leverage") or 1)
    amount     = trigger.get("trade_amount_usdt") or 0.0

    if not user_id or not amount:
        return

    user_settings = await queries.get_user_settings(db, user_id)
    if not user_settings:
        return

    key    = safe_decrypt(user_settings.get("coindcx_api_key_enc"))
    secret = safe_decrypt(user_settings.get("coindcx_api_secret_enc"))
    if not key or not secret:
        _log.warning("Trigger %d (futures): no CoinDCX API keys", trigger_id)
        return

    from exchange.auto_trade import _get_user_telegram  # noqa: PLC0415
    tg_bot, tg_chat_id = await _get_user_telegram(user_settings)

    # Signal → position side
    new_side = "long" if signal.direction == "BUY" else "short"
    close_side = "sell" if new_side == "long" else "buy"  # order to close opposite

    try:
        async with aiohttp.ClientSession() as session:
            exchange = CoinDCXClient(api_key=key, api_secret=secret, session=session)

            # ── Check existing open position for this trigger ─────────────────
            existing = await queries.get_open_futures_position(db, trigger_id)

            if existing:
                if existing["side"] == new_side:
                    _log.info(
                        "Trigger %d: already %s — skip duplicate futures signal",
                        trigger_id, new_side.upper(),
                    )
                    return
                # Opposite side open → close it first
                _log.info(
                    "Trigger %d: closing existing %s before opening %s",
                    trigger_id, existing["side"].upper(), new_side.upper(),
                )
                try:
                    close_result = await exchange.close_futures_position(
                        side=close_side,
                        pair=symbol,
                        quantity=existing["quantity"],
                        leverage=leverage,
                    )
                    close_order_id = str(
                        close_result.get("id") or close_result.get("order_id") or ""
                    )
                    # Compute rough P&L for the closed position
                    ep    = existing["entry_price"]
                    pnl_pct = (
                        (signal.entry_price - ep) / ep * 100 * leverage
                        if existing["side"] == "long"
                        else (ep - signal.entry_price) / ep * 100 * leverage
                    )
                    pnl_usdt = round(existing["margin_usdt"] * pnl_pct / 100, 4)
                    await queries.close_futures_position(
                        db, existing["id"],
                        close_price=signal.entry_price,
                        pnl_pct=round(pnl_pct, 4),
                        pnl_usdt=pnl_usdt,
                        cdx_close_order_id=close_order_id,
                    )
                    _log.info(
                        "Trigger %d: closed %s position  P&L=%.2f%%  ($%.4f)",
                        trigger_id, existing["side"].upper(), pnl_pct, pnl_usdt,
                    )
                except Exception as exc:
                    _log.error("Trigger %d: failed to close existing position: %s", trigger_id, exc)
                    return  # don't open new position if we couldn't close old one

            # ── Adaptive sizing ───────────────────────────────────────────────
            pos_before  = await queries.get_open_futures_position(db, trigger_id)
            usdt_spent  = pos_before["margin_usdt"] if pos_before else 0.0
            remaining   = max(round(amount - usdt_spent, 4), 0.0)

            if remaining < 0.50:
                _log.info("Trigger %d (futures): budget exhausted (remaining=$%.4f)", trigger_id, remaining)
                return

            adaptive_pct   = _adaptive_engine.buy_pct_for(signal.confidence, signal.adx_val)
            margin_usdt    = min(round(remaining * adaptive_pct / 100.0, 4), remaining)

            # ── Funding rate soft filter ──────────────────────────────────────
            try:
                funding = await exchange.get_futures_funding_rate(symbol)
                if funding is not None:
                    if new_side == "long" and funding > FUNDING_SKEW_PCT:
                        margin_usdt = round(margin_usdt * 0.80, 4)
                        _log.info(
                            "Trigger %d: funding rate %.4f%% > threshold → reducing LONG margin 20%%",
                            trigger_id, funding * 100,
                        )
                    elif new_side == "short" and funding < -FUNDING_SKEW_PCT:
                        margin_usdt = round(margin_usdt * 0.80, 4)
                        _log.info(
                            "Trigger %d: funding rate %.4f%% < -threshold → reducing SHORT margin 20%%",
                            trigger_id, funding * 100,
                        )
            except Exception:
                pass  # funding rate is optional

            MIN_ORDER_USDT = 10.0  # CoinDCX minimum notional
            if margin_usdt < MIN_ORDER_USDT:
                _log.warning(
                    "Trigger %d (futures): margin slice $%.2f is below CoinDCX minimum $%.2f "
                    "— increase trigger budget or leverage",
                    trigger_id, margin_usdt, MIN_ORDER_USDT,
                )
                if tg_bot and tg_chat_id:
                    from telegram_bot.alerts import send_trade_error
                    try:
                        await send_trade_error(
                            tg_bot, tg_chat_id, trigger_id, symbol, "FUTURES",
                            f"Margin slice ${margin_usdt:.2f} < minimum ${MIN_ORDER_USDT:.0f} "
                            f"(budget=${amount:.0f}, leverage={leverage}×). "
                            f"Increase trigger budget.",
                        )
                    except Exception:
                        pass
                return

            # Notional = margin × leverage; quantity = notional / price
            notional = round(margin_usdt * leverage, 4)
            qty_raw  = notional / signal.entry_price
            # Floor to 6 decimal places (handles small BTC positions like 0.000039)
            qty = math.floor(qty_raw * 1_000_000) / 1_000_000
            if qty <= 0:
                _log.warning(
                    "Trigger %d (futures): quantity rounds to 0 "
                    "(margin=$%.4f leverage=%dx notional=$%.4f price=$%.2f) — increase budget or leverage",
                    trigger_id, margin_usdt, leverage, notional, signal.entry_price,
                )
                return

            # ── ATR-based SL/TP ───────────────────────────────────────────────
            atr_pct = await _get_atr_pct(db, symbol, interval, signal.entry_price)
            sl_price, tp_price, liq_price = compute_futures_sl_tp(
                new_side, signal.entry_price, leverage, atr_pct
            )

            from exchange.coindcx_client import CoinDCXClient as _C
            futures_pair = _C._futures_pair(symbol)
            _log.info(
                "Trigger %d FUTURES %s %s (pair=%s): qty=%.6f leverage=%dx margin=$%.2f "
                "ATR=%.2f%% SL=%.4f TP=%.4f LIQ=%.4f",
                trigger_id, new_side.upper(), symbol, futures_pair,
                qty, leverage, margin_usdt,
                atr_pct, sl_price, tp_price, liq_price,
            )

            # ── Place order ───────────────────────────────────────────────────
            cdx_side = "buy" if new_side == "long" else "sell"
            result   = await exchange.create_futures_order(
                side=cdx_side,
                pair=symbol,
                order_type="market_order",
                quantity=qty,
                leverage=leverage,
            )
            cdx_order_id = str(result.get("id") or result.get("order_id") or "")

            # ── Record in DB ──────────────────────────────────────────────────
            pos_id = await queries.insert_futures_position(
                db,
                trigger_id=trigger_id,
                user_id=user_id,
                symbol=symbol,
                side=new_side,
                quantity=qty,
                entry_price=signal.entry_price,
                leverage=leverage,
                margin_usdt=margin_usdt,
                liquidation_price=liq_price,
                sl_price=sl_price,
                tp_price=tp_price,
                cdx_order_id=cdx_order_id,
            )

            _log.info(
                "Trigger %d: futures position #%d opened  %s %s  order=%s",
                trigger_id, pos_id, new_side.upper(), symbol, cdx_order_id,
            )

            # ── Telegram ──────────────────────────────────────────────────────
            if tg_bot and tg_chat_id:
                try:
                    await send_futures_trade_notification(
                        tg_bot, tg_chat_id,
                        side=new_side,
                        symbol=symbol,
                        quantity=qty,
                        entry_price=signal.entry_price,
                        leverage=leverage,
                        margin_usdt=margin_usdt,
                        sl_price=sl_price,
                        tp_price=tp_price,
                        liq_price=liq_price,
                        trigger_id=trigger_id,
                        reason=reason,
                    )
                except Exception as exc:
                    _log.warning("Futures Telegram alert failed: %s", exc)

    except Exception as exc:
        import traceback
        _log.error(
            "Futures trade failed for trigger %d: %s\n%s",
            trigger_id, exc, traceback.format_exc(),
        )
