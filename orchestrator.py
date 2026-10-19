"""
Main orchestrator — starts all services concurrently:
  1. SQLite database
  2. CoinDCX exchange client (order execution)
  3. Telegram bot (alerts + manual trading)
  4. FastAPI + Uvicorn (dashboard + WebSocket streaming)
  5. Background signal worker (detects signals & fires trigger alerts 24/7)

Run:
    python orchestrator.py

.env file must exist with all required keys (see .env.example).
"""

import asyncio
import logging
import os
import time

import aiohttp
import uvicorn

from config import load_settings
from database.db import init_db
from exchange.coindcx_client import CoinDCXClient
from log_setup import journal, setup_logging
from telegram_bot.bot import create_bot
from ui.server import app as fastapi_app

setup_logging()   # console + logs/bot.log + logs/decisions.jsonl (survive restarts)
logging.getLogger("coindcx_client").setLevel(logging.ERROR)  # always show request/response on API failure
log = logging.getLogger("orchestrator")

# Attach a protective stop to any futures position found without one (% from entry).
# 0 = alert only. Off by default because it would also touch manual positions.
AUTO_PROTECT_PCT = float(os.environ.get("FUTURES_AUTO_PROTECT_PCT", "0") or 0)


# ── Background signal worker ───────────────────────────────────────────────────

async def _update_trailing_sl(symbol: str, current_price: float, db) -> None:
    """
    Ratchet stop-loss upward for profitable open spot positions.

    Logic (runs on every closed candle):
      - If current_price >= avg_entry * (1 + TRAILING_ACTIVATION_PCT / 100):
          new_sl = current_price * (1 - TRAILING_DISTANCE_PCT / 100)
          Only commit if new_sl > existing stop_loss_price (never lower the SL).
    """
    from database import queries
    from exchange.auto_trade import TRAILING_ACTIVATION_PCT, TRAILING_DISTANCE_PCT

    positions = await queries.get_positions_for_sl_tp_check(db, symbol)
    for pos in positions:
        avg_entry = pos.get("avg_entry") or 0.0
        sl_price  = pos.get("stop_loss_price") or 0.0
        tp_price  = pos.get("take_profit_price") or 0.0
        if avg_entry <= 0:
            continue

        activation_price = avg_entry * (1 + TRAILING_ACTIVATION_PCT / 100)
        if current_price < activation_price:
            continue  # not yet in trailing zone

        new_sl = current_price * (1 - TRAILING_DISTANCE_PCT / 100)
        if new_sl <= sl_price:
            continue  # already at or above this level — nothing to raise

        # Raise the SL (preserve coins_held, avg_entry, usdt_spent, tp unchanged)
        await queries.update_spot_sl(
            db,
            trigger_id=pos["trigger_id"],
            new_sl_price=round(new_sl, 8),
        )
        log.info(
            "Trailing SL raised: trigger=%d %s  old_sl=%.4f → new_sl=%.4f  "
            "(price=%.4f  entry=%.4f)",
            pos["trigger_id"], symbol.upper(), sl_price, new_sl,
            current_price, avg_entry,
        )


async def _check_mtf_ema_alignment(symbol: str, signal_direction: str) -> bool:
    """
    Fetch 60 15-minute candles and check that EMA(20) vs EMA(50) aligns with
    the signal direction.  Returns True (proceed) on any data/network error so
    we never silently block a trade due to a fetch failure.

    BUY  signal → require 15m EMA(20) > EMA(50)  (uptrend context)
    SELL signal → require 15m EMA(20) < EMA(50)  (downtrend context)
    """
    try:
        from streaming.stream import fetch_historical_klines
        from indicators.indicators import ema as calc_ema

        klines = await fetch_historical_klines(symbol, "15m", limit=60)
        if len(klines) < 52:   # need ≥ EMA_SLOW (50) + warm-up bars
            return True

        closes    = [k.close for k in klines]
        ema20     = calc_ema(closes, 20)
        ema50     = calc_ema(closes, 50)
        last_ema20 = next((v for v in reversed(ema20) if v is not None), None)
        last_ema50 = next((v for v in reversed(ema50) if v is not None), None)

        if last_ema20 is None or last_ema50 is None:
            return True

        if signal_direction == "BUY":
            return last_ema20 > last_ema50   # uptrend required for buys
        else:
            return last_ema20 < last_ema50   # downtrend required for sells

    except Exception as exc:
        log.debug("MTF EMA check failed for %s (%s) — allowing trade", symbol, exc)
        return True  # fail-open: never silently block on network error


# Positions with a close already in flight. Every (symbol, interval) worker runs
# the SL/TP check, so e.g. BTC 1m and BTC 5m workers both see the same open
# position at a 5-minute boundary. Without this guard both send a close order,
# and the second one opens an unintended position in the opposite direction.
_closing_spot_triggers: set[int] = set()
_closing_futures_positions: set[int] = set()


async def _run_spot_close(coro, trigger_id: int) -> None:
    try:
        await coro
    finally:
        _closing_spot_triggers.discard(trigger_id)


async def _check_sl_tp(symbol: str, current_price: float, db, settings) -> None:
    """
    After every closed candle, check all open spot and futures positions on this
    symbol against their stop-loss / take-profit prices and auto-close when triggered.
    """
    from database import queries
    from exchange.auto_trade import execute_trigger_trade
    from signals.detector import Signal
    import time as _time

    # ── Trailing SL update (ratchets SL upward for profitable positions) ──────
    try:
        await _update_trailing_sl(symbol, current_price, db)
    except Exception as exc:
        log.error("Trailing SL update error %s: %s", symbol.upper(), exc)

    # ── Spot SL/TP ────────────────────────────────────────────────────────────
    positions = await queries.get_positions_for_sl_tp_check(db, symbol)
    for pos in positions:
        trigger_id = pos["trigger_id"]
        sl_price   = pos.get("stop_loss_price")
        tp_price   = pos.get("take_profit_price")

        reason = None
        if sl_price and current_price <= sl_price:
            reason = "stop_loss"
        elif tp_price and current_price >= tp_price:
            reason = "take_profit"

        if not reason or trigger_id in _closing_spot_triggers:
            continue

        log.info(
            "Spot SL/TP triggered: trigger=%d %s price=%.4f reason=%s",
            trigger_id, symbol.upper(), current_price, reason,
        )

        trigger = await queries.get_trigger(db, trigger_id)
        if not trigger or not trigger.get("active"):
            continue

        fake_signal = Signal(
            direction="SELL",
            confidence="HIGH",
            entry_price=current_price,
            open_time=int(_time.time() * 1000),
            macd_val=0.0, signal_val=0.0, histogram=0.0,
            adx_val=0.0, trend_note=reason,
            reasons=[reason],
        )
        _closing_spot_triggers.add(trigger_id)
        asyncio.create_task(_run_spot_close(
            execute_trigger_trade(db, trigger, fake_signal, symbol.upper(),
                                  trigger["interval"], signal_id=None, reason=reason),
            trigger_id,
        ))

    # ── Futures SL/TP ─────────────────────────────────────────────────────────
    futures_positions = await queries.get_open_futures_positions_for_sl_tp(db, symbol)
    for fpos in futures_positions:
        sl_price = fpos["sl_price"]
        tp_price = fpos["tp_price"]
        side     = fpos["side"]   # 'long' or 'short'

        reason = None
        if side == "long":
            if current_price <= sl_price:
                reason = "stop_loss"
            elif current_price >= tp_price:
                reason = "take_profit"
        else:  # short
            if current_price >= sl_price:
                reason = "stop_loss"
            elif current_price <= tp_price:
                reason = "take_profit"

        if not reason or fpos["id"] in _closing_futures_positions:
            continue

        _closing_futures_positions.add(fpos["id"])
        log.info(
            "Futures SL/TP triggered: pos=%d %s %s price=%.4f reason=%s (SL=%.4f TP=%.4f)",
            fpos["id"], side.upper(), symbol.upper(), current_price, reason,
            sl_price, tp_price,
        )

        asyncio.create_task(
            _close_futures_position_sltp(db, fpos, current_price, reason)
        )


async def _close_futures_position_sltp(db, fpos: dict, close_price: float, reason: str) -> None:
    """Close a futures position due to SL/TP trigger."""
    import aiohttp
    from database import queries
    from auth.encryption import safe_decrypt
    from exchange.coindcx_client import CoinDCXClient
    from exchange.futures_trade import compute_futures_pnl, safe_close_futures
    from telegram_bot.alerts import send_futures_close_notification

    trigger_id = fpos["trigger_id"]
    user_id    = fpos["user_id"]
    side       = fpos["side"]

    try:
        user_settings = await queries.get_user_settings(db, user_id)
        key    = safe_decrypt((user_settings or {}).get("coindcx_api_key_enc"))
        secret = safe_decrypt((user_settings or {}).get("coindcx_api_secret_enc"))
        if not key or not secret:
            return

        async with aiohttp.ClientSession() as session:
            exchange = CoinDCXClient(api_key=key, api_secret=secret, session=session)
            close_order_id, already_closed = await safe_close_futures(exchange, fpos)

        if already_closed:
            # The exchange-side SL/TP placed with the order fired first. Record the
            # close (price is the candle close — an estimate of the actual fill).
            reason = f"{reason} (exchange)"
            log.info("Futures position %d already closed on exchange — recording only", fpos["id"])

        ep = fpos["entry_price"]
        pnl_pct, pnl_usdt = compute_futures_pnl(
            side, ep, close_price, fpos["leverage"], fpos["margin_usdt"]
        )

        await queries.close_futures_position(
            db, fpos["id"], close_price, pnl_pct, pnl_usdt, close_order_id
        )
        log.info(
            "Futures position %d closed (%s): price=%.4f P&L=%.2f%% ($%.4f)",
            fpos["id"], reason, close_price, pnl_pct, pnl_usdt,
        )
        journal("futures_closed", trigger_id=trigger_id, symbol=fpos["symbol"], side=side,
                reason=reason, price=close_price, pnl_pct=pnl_pct, pnl_usdt=pnl_usdt)

        # Telegram
        from exchange.auto_trade import _get_user_telegram  # noqa: PLC0415
        tg_bot, tg_chat_id = await _get_user_telegram(user_settings)
        if tg_bot and tg_chat_id:
            await send_futures_close_notification(
                tg_bot, tg_chat_id,
                side=side, symbol=fpos["symbol"],
                quantity=fpos["quantity"],
                entry_price=ep, close_price=close_price,
                leverage=fpos["leverage"],
                margin_usdt=fpos["margin_usdt"],
                pnl_pct=pnl_pct, pnl_usdt=pnl_usdt,
                trigger_id=trigger_id, reason=reason,
            )
    except Exception as exc:
        import traceback
        log.error(
            "Failed to close futures position %d: %s\n%s",
            fpos["id"], exc, traceback.format_exc(),
        )
    finally:
        _closing_futures_positions.discard(fpos["id"])


async def _stream_pair(symbol: str, interval: str, db, tg_bot, exchange, settings) -> None:
    """
    Stream one symbol/interval pair forever, detect signals, and fire
    Telegram alerts whenever an active trigger matches.
    Restarts automatically on any error with exponential backoff.
    """
    from streaming.stream import stream_klines, fetch_historical_klines
    from signals.detector import SignalDetector, params_for_interval
    from database import queries
    from telegram_bot.alerts import send_signal_alert

    retry_delay = 10  # seconds; doubles on each crash, capped at 300 s

    while True:
        try:
            detector = SignalDetector(**params_for_interval(interval))
            log.info("BG worker params  %s %s  tier=%s",
                     symbol.upper(), interval,
                     "scalping" if params_for_interval(interval)["adx_threshold"] <= 12 else
                     "intraday" if params_for_interval(interval)["adx_threshold"] <= 18 else
                     "swing"    if params_for_interval(interval)["adx_threshold"] <= 20 else
                     "position")
            historical = await fetch_historical_klines(symbol, interval, limit=201)
            detector.seed(historical)
            log.info("BG worker seeded  %s %s  (%d bars)", symbol.upper(), interval, len(historical))

            candle_count = 0
            async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=False):
                if not kline.is_closed:
                    continue

                candle_count += 1
                signal = detector.update(kline)

                # Persist the candle so futures ATR-based SL/TP has fresh data
                # even when no dashboard tab is open.
                try:
                    await queries.insert_candle(db, symbol, interval, kline)
                except Exception as exc:
                    log.warning("Candle persist failed %s %s: %s", symbol.upper(), interval, exc)
                # Log every 30 candles so we can confirm the worker is alive and processing
                if candle_count % 30 == 0:
                    snap = detector.current_snapshot()
                    log.info("BG heartbeat  %s %s  candle#%d  close=%.4f  adx=%s",
                             symbol.upper(), interval, candle_count, kline.close,
                             f"{snap.adx_val:.1f}" if snap.adx_val else "n/a")

                # ── Stop-loss / take-profit check (every candle) ─────────────
                try:
                    await _check_sl_tp(symbol, kline.close, db, settings)
                except Exception as exc:
                    log.error("SL/TP check error %s: %s", symbol.upper(), exc)

                # ── Futures trailing stop / breakeven (every candle) ──────────
                try:
                    from exchange.futures_monitor import monitor_futures_positions
                    await monitor_futures_positions(db, symbol, kline.close)
                except Exception as exc:
                    log.error("Futures monitor error %s: %s", symbol.upper(), exc)

                if signal is None:
                    continue

                log.info("BG signal  %s %s %s %s @ %.2f",
                         symbol.upper(), interval, signal.direction,
                         signal.confidence, signal.entry_price)
                journal("signal", symbol=symbol.upper(), interval=interval, direction=signal.direction,
                        confidence=signal.confidence, price=signal.entry_price, adx=signal.adx_val,
                        is_exit=getattr(signal, "is_exit", False), reasons=signal.reasons,
                        trend=signal.trend_note)

                # Persist to DB and capture signal_id for trade linkage
                signal_id = None
                try:
                    signal_id = await queries.insert_signal(db, symbol, interval, signal)
                except Exception:
                    pass

                # Fire Telegram alert and execute auto-trades for matched triggers
                try:
                    from exchange.auto_trade import execute_trigger_trade
                    active_triggers = await queries.get_triggers(db, active_only=True)
                    matched_triggers = [
                        t for t in active_triggers
                        if t.get("user_id") is not None
                        and queries.trigger_matches(t, symbol, interval, signal.confidence, signal.adx_val)
                    ]
                    if matched_triggers:

                        # Send Telegram alert once (uses first matched trigger's user settings)
                        try:
                            user_settings = await queries.get_user_settings(db, matched_triggers[0]["user_id"])
                            from auth.encryption import safe_decrypt
                            tg_token   = safe_decrypt((user_settings or {}).get("telegram_token"))
                            tg_chat_id = safe_decrypt((user_settings or {}).get("telegram_chat_id"))
                            if tg_token and tg_chat_id:
                                from telegram import Bot
                                per_user_bot = Bot(token=tg_token)
                                await send_signal_alert(
                                    bot=per_user_bot,
                                    chat_id=tg_chat_id,
                                    signal=signal,
                                    symbol=symbol.upper(),
                                    interval=interval,
                                    phase=settings.trading_phase,
                                    db=db,
                                    exchange=exchange,
                                    settings=settings,
                                )
                            else:
                                # Fall back to global bot
                                await send_signal_alert(
                                    bot=tg_bot,
                                    chat_id=settings.telegram_chat_id,
                                    signal=signal,
                                    symbol=symbol.upper(),
                                    interval=interval,
                                    phase=settings.trading_phase,
                                    db=db,
                                    exchange=exchange,
                                    settings=settings,
                                )
                        except Exception as exc:
                            log.error("BG Telegram alert error: %s", exc)

                        # MTF EMA alignment filter — only trade with the 15m trend.
                        # Checked once per signal (not per trigger) to save API calls.
                        # Fail-open: if fetch fails, trade proceeds normally.
                        try:
                            mtf_aligned = await _check_mtf_ema_alignment(
                                symbol.upper(), signal.direction
                            )
                        except Exception:
                            mtf_aligned = True
                        if not mtf_aligned and not getattr(signal, "is_exit", False):
                            log.info(
                                "BG MTF filter blocked %s %s signal on %s — 15m EMA trend disagrees",
                                signal.direction, signal.confidence, symbol.upper(),
                            )
                            journal("signal_blocked", symbol=symbol.upper(), interval=interval,
                                    direction=signal.direction, reason="15m EMA20/50 trend disagrees")
                            continue  # skip all triggers for this signal

                        # Execute trade for each matched trigger.
                        # Position check (DB vs exchange balance) is handled inside
                        # execute_trigger_trade — no pre-filtering here so that manual
                        # holdings (bought outside the bot) can also be sold.
                        for trig in matched_triggers:
                            mtype = (trig.get("market_type") or "spot").lower()
                            if mtype == "futures":
                                from exchange.futures_trade import execute_futures_trigger_trade
                                asyncio.create_task(
                                    execute_futures_trigger_trade(
                                        db, trig, signal, symbol.upper(), interval, signal_id
                                    )
                                )
                            else:
                                asyncio.create_task(
                                    execute_trigger_trade(
                                        db, trig, signal, symbol.upper(), interval, signal_id
                                    )
                                )
                            log.info(
                                "BG auto-trade queued  trigger=%d  %s %s %s %s [%s]",
                                trig["id"], signal.direction, signal.confidence,
                                symbol.upper(), interval, mtype,
                            )
                except Exception as exc:
                    log.error("BG trigger check error: %s", exc)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("BG worker %s %s crashed (%s) — restarting in %ds",
                        symbol.upper(), interval, exc, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 300)  # exponential backoff, cap 5 min
            continue

        # Successful run — reset backoff
        retry_delay = 10


async def _ict_worker(db, trigger: dict) -> None:
    """Run one ICT/SMT trigger forever, restarting with backoff on failure."""
    from exchange.ict_runner import run_ict_trigger
    delay = 10
    while True:
        try:
            await run_ict_trigger(db, trigger)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ICT worker #%d crashed (%s) — restarting in %ds", trigger["id"], exc, delay)
            journal("ict_worker_crash", trigger_id=trigger["id"], error=str(exc)[:300])
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


def _ict_key(t: dict) -> tuple:
    """Restart the worker whenever a setting it captured at start changes."""
    return ("ict", t["id"], t["symbol"].upper(), t.get("strategy_mode"), t.get("leverage"),
            t.get("trade_amount_usdt"), t.get("allow_shorts"))


async def background_signal_worker(db, tg_bot, exchange, settings) -> None:
    """
    Manage one streaming task per unique (symbol, interval) across all active
    indicator triggers, plus one task per ICT/SMT trigger. Re-checks the DB every
    30 s so newly added/removed triggers are picked up without a restart.
    """
    from database import queries

    running: dict[tuple, asyncio.Task] = {}

    while True:
        try:
            triggers = [t for t in await queries.get_triggers(db, active_only=True)
                        if t.get("user_id") is not None]   # only triggers that belong to a real user
            pairs = {
                (t["symbol"].lower(), t["interval"])
                for t in triggers
                if (t.get("strategy") or "macd") == "macd"
            }
            ict = {_ict_key(t): t for t in triggers if t.get("strategy") == "ict_smt"}

            # Start tasks for new pairs
            for sym, iv in pairs:
                key = (sym, iv)
                if key not in running or running[key].done():
                    log.info("BG worker starting %s %s", sym.upper(), iv)
                    running[key] = asyncio.create_task(
                        _stream_pair(sym, iv, db, tg_bot, exchange, settings)
                    )
            for key, trig in ict.items():
                if key not in running or running[key].done():
                    log.info("ICT worker starting trigger #%d %s [%s]", trig["id"], trig["symbol"],
                             trig.get("strategy_mode"))
                    running[key] = asyncio.create_task(_ict_worker(db, trig))

            # Cancel tasks whose trigger is gone, disabled or reconfigured
            for key in list(running):
                if key not in pairs and key not in ict and not running[key].done():
                    log.info("BG worker stopping %s", key)
                    running[key].cancel()
                    del running[key]

        except asyncio.CancelledError:
            for task in running.values():
                task.cancel()
            raise
        except Exception as exc:
            log.error("BG worker manager error: %s", exc)

        await asyncio.sleep(30)   # re-sync with DB triggers every 30 s


async def position_watchdog(db, interval_s: int = 300) -> None:
    """
    Every 5 min: any live futures position without an exchange stop-loss gets a
    Telegram alert (once an hour per position). The worst losses on this account —
    a manual ETH long left without a stop (−$10), an INR liquidation, and a naked
    long opened by the old close logic — were all positions with no stop.
    """
    import aiohttp
    from auth.encryption import safe_decrypt
    from database import queries
    from exchange.account_ledger import open_position_rows
    from exchange.auto_trade import _get_user_telegram
    from telegram_bot.alerts import send_unprotected_position_alert

    alerted: dict[tuple, float] = {}
    while True:
        await asyncio.sleep(interval_s)
        try:
            for user in await queries.get_all_users(db):
                us = await queries.get_user_settings(db, user["id"]) or {}
                key, secret = safe_decrypt(us.get("coindcx_api_key_enc")), safe_decrypt(us.get("coindcx_api_secret_enc"))
                if not key or not secret:
                    continue
                async with aiohttp.ClientSession() as session:
                    client = CoinDCXClient(api_key=key, api_secret=secret, session=session)
                    raw = []
                    for cur in ("USDT", "INR"):
                        try:
                            raw += await client.list_futures_positions(cur)
                        except Exception:
                            pass
                    for p in open_position_rows(raw):
                        if p["sl"]:
                            continue
                        k = (user["id"], p["pair"], p["side"])
                        if time.time() - alerted.get(k, 0) < 3600:
                            continue
                        alerted[k] = time.time()
                        journal("unprotected_position", user_id=user["id"], **{x: p[x] for x in
                                ("pair", "side", "qty", "avg", "mark", "upnl", "liq")})
                        tg_bot, chat = await _get_user_telegram(us)
                        if tg_bot:
                            await send_unprotected_position_alert(tg_bot, chat, p)
                        if AUTO_PROTECT_PCT > 0 and p["avg"] > 0:
                            try:
                                sym = p["pair"].replace("B-", "").replace("_", "")
                                tick = float((await client.get_futures_instrument(sym, p["margin_currency"]))
                                             .get("price_increment") or 0.1)
                                f = 1 - AUTO_PROTECT_PCT / 100 if p["side"] == "long" else 1 + AUTO_PROTECT_PCT / 100
                                sl = round(round(p["avg"] * f / tick) * tick, 8)
                                await client.create_futures_tpsl(str(p["id"]), sl_price=sl)
                                journal("auto_protect", user_id=user["id"], pair=p["pair"], sl=sl)
                            except Exception as exc:
                                journal("auto_protect_failed", pair=p["pair"], error=str(exc)[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Position watchdog error: %s", exc)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # ── Load config ──────────────────────────────────────────────────────────
    settings = load_settings()
    log.info("Config loaded — phase=%d  symbol=%s  interval=%s",
             settings.trading_phase, settings.symbol, settings.interval)

    # ── Database ─────────────────────────────────────────────────────────────
    db = await init_db(settings.db_path)
    log.info("SQLite ready at %s", settings.db_path)

    # ── HTTP session (shared across exchange + any async HTTP calls) ──────────
    session = aiohttp.ClientSession()

    # ── CoinDCX client ────────────────────────────────────────────────────────
    exchange = CoinDCXClient(
        api_key=settings.coindcx_api_key,
        api_secret=settings.coindcx_api_secret,
        session=session,
    )
    log.info("CoinDCX client ready")

    # ── Telegram bot ──────────────────────────────────────────────────────────
    tg_app = await create_bot(settings, db, exchange)
    log.info("Telegram bot configured")

    # ── Inject dependencies into FastAPI app.state ───────────────────────────
    fastapi_app.state.db            = db
    fastapi_app.state.exchange      = exchange
    fastapi_app.state.telegram_bot  = tg_app.bot
    fastapi_app.state.settings      = settings

    # ── Start everything ──────────────────────────────────────────────────────
    uvicorn_config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=settings.port,
        log_level="warning",
    )
    server = uvicorn.Server(uvicorn_config)

    log.info("Starting dashboard on http://0.0.0.0:%d", settings.port)
    log.info("Starting Telegram bot polling")

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)

        # Start background signal worker (runs forever, independent of UI)
        worker_task = asyncio.create_task(
            background_signal_worker(db, tg_app.bot, exchange, settings)
        )
        watchdog_task = asyncio.create_task(position_watchdog(db))
        log.info("Background signal worker + position watchdog started (logs in %s/)",
                 os.environ.get("LOG_DIR", "logs"))

        try:
            await server.serve()
        finally:
            log.info("Shutting down…")
            worker_task.cancel()
            watchdog_task.cancel()
            await asyncio.gather(worker_task, watchdog_task, return_exceptions=True)
            await tg_app.updater.stop()
            await tg_app.stop()

    await session.close()
    await db.close()
    log.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
