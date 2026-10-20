"""
Daily trend-following trigger runner (signals.trend) — spot, long or flat.

Once per day, just after the 00:00 UTC daily close (05:30 IST), each of the three
systems checks the closed candle:
  flat + close above its N-day high  → buy one third of the trigger budget
  long + close below its M-day low   → sell exactly what that system bought
Entries only happen on a fresh breakout; a trigger created mid-trend waits for the
next one instead of buying late with a distant exit.

Modes (triggers.strategy_mode): 'paper' records simulated fills at the daily close;
'live' places CoinDCX spot market orders and books the real fills and fees. A system
only ever sells the coins it bought itself (capped to the actual balance).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Optional

from exchange.fees import SPOT_FEE_RATE
from log_setup import journal
from signals.trend import SYSTEMS, decide, levels

_log = logging.getLogger("trend_runner")
_HISTORY_DAYS = 160


def _floor(x: float, step: float) -> float:
    dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(x / step + 1e-9) * step, dp)


class TrendRunner:
    def __init__(self, db, trigger: dict) -> None:
        self.db = db
        self.trigger = trigger
        self.tid = trigger["id"]
        self.user_id = trigger.get("user_id")
        self.symbol = trigger["symbol"].upper()
        self.base = self.symbol.replace("USDT", "")
        self.mode = (trigger.get("strategy_mode") or "paper").lower()
        self.budget = float(trigger.get("trade_amount_usdt") or 0.0)
        self.step, self.min_notional = 1e-05, 5.0

    # ── plumbing (same pattern as the ICT runner) ─────────────────────────────

    async def _telegram(self):
        from database import queries
        from exchange.auto_trade import _get_user_telegram
        settings = await queries.get_user_settings(self.db, self.user_id) if self.user_id else None
        return await _get_user_telegram(settings or {})

    async def _notify(self, text: str) -> None:
        bot, chat = await self._telegram()
        if bot:
            from telegram.constants import ParseMode
            try:
                await bot.send_message(chat_id=chat, text=text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass

    async def _client_call(self, fn_name: str, *args, **kwargs):
        import aiohttp
        from auth.encryption import safe_decrypt
        from database import queries
        from exchange.coindcx_client import CoinDCXClient
        s = await queries.get_user_settings(self.db, self.user_id) or {}
        key, secret = safe_decrypt(s.get("coindcx_api_key_enc")), safe_decrypt(s.get("coindcx_api_secret_enc"))
        if not key or not secret:
            raise RuntimeError("no CoinDCX API keys configured")
        async with aiohttp.ClientSession() as session:
            return await getattr(CoinDCXClient(key, secret, session), fn_name)(*args, **kwargs)

    async def load_market(self) -> None:
        """Spot step size and minimum order value from markets_details."""
        try:
            import aiohttp
            from exchange.coindcx_client import CoinDCXClient
            async with aiohttp.ClientSession() as session:
                rows = await CoinDCXClient("", "", session)._get("/exchange/v1/markets_details")
            m = next(r for r in rows if r.get("coindcx_name") == self.symbol)
            self.step = float(m.get("step") or self.step)
            self.min_notional = float(m.get("min_notional") or self.min_notional)
        except Exception as exc:
            _log.warning("Trend #%d: market details unavailable (%s) — step=%g min=$%g",
                         self.tid, exc, self.step, self.min_notional)

    async def _open_rows(self) -> dict[str, dict]:
        cur = await self.db.execute(
            "SELECT * FROM strategy_trades WHERE trigger_id = ? AND status = 'open'", (self.tid,))
        out = {}
        for r in (dict(x) for x in await cur.fetchall()):
            out[json.loads(r["setup_json"] or "{}").get("system", "?")] = r
        return out

    # ── daily decision ────────────────────────────────────────────────────────

    async def evaluate(self, bars) -> list[str]:
        """Act on the last closed daily candle. Safe to call repeatedly for the same day."""
        if len(bars) < max(n for n, _ in SYSTEMS) + 1:
            return []
        highs, lows, closes = [b.high for b in bars], [b.low for b in bars], [b.close for b in bars]
        day = bars[-1].open_time // 1000
        price = closes[-1]
        held = await self._open_rows()
        done = []
        for n, m in SYSTEMS:
            key = f"{n}/{m}"
            row = held.get(key)
            action = decide(highs, lows, closes, n, m, holding=row is not None)
            hi, lo = levels(highs, lows, n, m)
            journal("trend_check", trigger_id=self.tid, symbol=self.symbol, system=key, close=price,
                    entry_level=hi, exit_level=lo, holding=row is not None, action=action)
            if action == "buy":
                if await self._bought_on(key, day):
                    continue
                if await self._buy(key, price, lo, day):
                    done.append(f"BUY {key}")
            elif action == "sell" and row:
                if await self._sell(row, key, price):
                    done.append(f"SELL {key}")
        return done

    async def _bought_on(self, key: str, day: int) -> bool:
        """Did this system already act on this daily candle (restart / repeated evaluation)?"""
        cur = await self.db.execute("SELECT setup_json FROM strategy_trades WHERE trigger_id = ?", (self.tid,))
        for (sj,) in await cur.fetchall():
            meta = json.loads(sj or "{}")
            if meta.get("system") == key and meta.get("decided_on") == day:
                return True
        return False

    async def _buy(self, key: str, price: float, exit_level: float, day: int) -> bool:
        from database import queries
        spend = self.budget / len(SYSTEMS)
        qty = _floor(spend / (price * (1 + SPOT_FEE_RATE)), self.step)
        if qty * price < self.min_notional:
            journal("trend_skipped", trigger_id=self.tid, system=key,
                    reason=f"${spend:.2f} per system is below the ${self.min_notional:g} minimum")
            return False
        fill_px, fill_qty, fee, order_id = price, qty, qty * price * SPOT_FEE_RATE, None
        if self.mode == "live":
            try:
                bals = await self._client_call("get_balances")
                usdt = next((float(b["balance"]) for b in bals if b.get("currency") == "USDT"), 0.0)
                if usdt < qty * price * (1 + SPOT_FEE_RATE):
                    await self._notify(f"⚠️ *Trend #{self.tid}* `{self.symbol}` breakout ({key}) but spot USDT "
                                       f"is ${usdt:.2f} — need ${spend:.2f}. Convert INR→USDT on CoinDCX.")
                    journal("trend_skipped", trigger_id=self.tid, system=key, reason=f"spot USDT {usdt:.2f}")
                    return False
                res = await self._client_call("create_order", side="buy", market=self.symbol,
                                              order_type="market_order", quantity=qty)
                order_id = _order_id(res)
                fill_px, fill_qty, fee = await self._fills(order_id, price, qty)
            except Exception as exc:
                journal("trend_order_failed", trigger_id=self.tid, system=key, side="buy", error=str(exc)[:300])
                await self._notify(f"⚠️ *Trend #{self.tid}* BUY {self.symbol} ({key}) failed: {str(exc)[:200]}")
                return False
        await queries.insert_strategy_trade(
            self.db, trigger_id=self.tid, user_id=self.user_id, strategy="trend_donchian", mode=self.mode,
            symbol=self.symbol, side="long", entry_price=fill_px, sl_price=exit_level, tp_price=0.0,
            quantity=fill_qty, leverage=1, risk_usdt=round(max(fill_px - exit_level, 0) * fill_qty, 4),
            status="open", cdx_order_id=order_id, filled_at=int(time.time()), fees_usdt=round(fee, 6),
            setup_json=json.dumps({"system": key, "decided_on": day}))
        journal("trend_buy", trigger_id=self.tid, symbol=self.symbol, mode=self.mode, system=key,
                price=fill_px, qty=fill_qty, fee=fee, exit_level=exit_level, order_id=order_id)
        await self._notify(f"🟢 *Trend BUY* `{self.symbol}` ({key} breakout) — Trigger #{self.tid} "
                           f"[{self.mode.upper()}]\n{fill_qty:g} @ {fill_px:,.2f} · exits on a daily close "
                           f"below the {key.split('/')[1]}-day low (now {exit_level:,.2f})")
        return True

    async def _sell(self, row: dict, key: str, price: float) -> bool:
        from database import queries
        qty = row["quantity"]
        fill_px, fee, order_id = price, qty * price * SPOT_FEE_RATE, None
        if self.mode == "live":
            try:
                bals = await self._client_call("get_balances")
                have = next((float(b["balance"]) for b in bals if b.get("currency") == self.base), 0.0)
                qty = _floor(min(qty, have), self.step)       # only this system's coins, never more
                if qty <= 0:
                    raise RuntimeError(f"no {self.base} left to sell (balance {have})")
                res = await self._client_call("create_order", side="sell", market=self.symbol,
                                              order_type="market_order", quantity=qty)
                order_id = _order_id(res)
                fill_px, qty, fee = await self._fills(order_id, price, qty)
            except Exception as exc:
                journal("trend_order_failed", trigger_id=self.tid, system=key, side="sell", error=str(exc)[:300])
                await self._notify(f"⚠️ *Trend #{self.tid}* SELL {self.symbol} ({key}) failed: {str(exc)[:200]}")
                return False
        fees = (row["fees_usdt"] or 0) + fee
        pnl = (fill_px - row["entry_price"]) * qty - fees
        risk = row["risk_usdt"] or 0
        await queries.update_strategy_trade(
            self.db, row["id"], status="closed", closed_at=int(time.time()), exit_price=fill_px,
            exit_reason=f"close below {key.split('/')[1]}-day low", pnl_usdt=round(pnl, 4),
            fees_usdt=round(fees, 6), r_multiple=round(pnl / risk, 3) if risk else 0.0)
        journal("trend_sell", trigger_id=self.tid, symbol=self.symbol, mode=self.mode, system=key,
                price=fill_px, qty=qty, pnl=round(pnl, 4), order_id=order_id)
        await self._notify(f"🔴 *Trend SELL* `{self.symbol}` ({key} exit) — Trigger #{self.tid} "
                           f"[{self.mode.upper()}]\n{qty:g} @ {fill_px:,.2f} · P&L ${pnl:+.4f} after fees")
        return True

    async def _fills(self, order_id: Optional[str], price: float, qty: float) -> tuple[float, float, float]:
        """Average price, quantity and fee from the exchange's fills for this order."""
        await asyncio.sleep(2)
        try:
            now = int(time.time() * 1000)
            rows = [f for f in await self._client_call("get_spot_fills", from_ms=now - 300_000, to_ms=now + 60_000)
                    if order_id and str(f.get("order_id")) == str(order_id)]
            q = sum(float(f["quantity"]) for f in rows)
            if q > 0:
                px = sum(float(f["quantity"]) * float(f["price"]) for f in rows) / q
                return px, q, sum(float(f.get("fee_amount") or 0) for f in rows)
        except Exception as exc:
            journal("trend_fill_lookup_failed", trigger_id=self.tid, error=str(exc)[:200])
        return price, qty, qty * price * SPOT_FEE_RATE


def _order_id(res) -> Optional[str]:
    if isinstance(res, dict) and isinstance(res.get("orders"), list) and res["orders"]:
        res = res["orders"][0]
    if isinstance(res, list) and res:
        res = res[0]
    if not isinstance(res, dict):
        return None
    oid = res.get("id") or res.get("order_id")
    return str(oid) if oid else None


async def run_trend_trigger(db, trigger: dict) -> None:
    """Evaluate after each daily close forever (also once at start-up, to catch up)."""
    from streaming.stream import _fetch_futures_klines_range_sync
    runner = TrendRunner(db, trigger)
    await runner.load_market()

    async def daily_bars():
        now = int(time.time() * 1000)
        return await asyncio.to_thread(_fetch_futures_klines_range_sync, runner.symbol, "1d",
                                       now - _HISTORY_DAYS * 86_400_000, now, True)   # spot candles

    first = True
    while True:
        try:
            bars = await daily_bars()
            acted = await runner.evaluate(bars)
            if first:
                hi_lo = [levels([b.high for b in bars], [b.low for b in bars], n, m) for n, m in SYSTEMS]
                held = await runner._open_rows()
                state = " · ".join(f"{n}/{m}: {'LONG' if f'{n}/{m}' in held else f'flat, buys > {hi:,.0f}'}"
                                   for (n, m), (hi, _) in zip(SYSTEMS, hi_lo))
                _log.info("Trend #%d %s ready [%s] — %s", runner.tid, runner.symbol, runner.mode, state)
                await runner._notify(f"📈 *Trend trigger #{runner.tid}* `{runner.symbol}` running "
                                     f"[{runner.mode.upper()}]\n{state}\nChecks each daily close (05:30 IST).")
                first = False
            if acted:
                _log.info("Trend #%d %s: %s", runner.tid, runner.symbol, ", ".join(acted))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("Trend #%d evaluation failed: %s", runner.tid, exc)
            await asyncio.sleep(300)
            continue
        await asyncio.sleep(86_400 - (time.time() % 86_400) + 90)   # 90 s after the next 00:00 UTC
