"""
ICT/SMT strategy runner — drives signals.ict_smt.ICTEngine for one trigger.

Modes (triggers.strategy_mode):
  paper — no orders. Fills and exits are simulated against live 1m futures candles
          with the same conservative simulator the backtest uses. Results go to
          strategy_trades, Telegram and the daily report, so the routine can be
          judged on live data before real money is involved. THIS IS THE DEFAULT.
  live  — resting limit entry at the FVG (maker fee) with exchange-side SL/TP on
          CoinDCX futures. After a fill the position is checked for an attached
          stop; if none is found one is created, and if that fails the position is
          closed immediately. Unfilled orders are cancelled at expiry.

Sizing: risk ICT_RISK_PCT % of the trigger budget per trade (default 1%). If the
exchange minimum size forces more risk than that, it is accepted up to
ICT_MAX_RISK_PCT (default 3%), otherwise the setup is skipped. Margin never
exceeds the trigger budget.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import math
import os
import time
from typing import Optional

from log_setup import journal
from signals.ict_smt import Bar, ICTConfig, ICTEngine, Setup, NY, ny_time_to_ms
from signals.ict_sim import SimCosts, SimTrade, step

_log = logging.getLogger("ict_runner")

RISK_PCT     = float(os.environ.get("ICT_RISK_PCT", "1.0"))
MAX_RISK_PCT = float(os.environ.get("ICT_MAX_RISK_PCT", "3.0"))
WARMUP_DAYS  = 10
REFERENCE    = {"BTCUSDT": "ETHUSDT", "ETHUSDT": "BTCUSDT"}


def reference_for(symbol: str) -> str:
    return REFERENCE.get(symbol.upper(), "BTCUSDT")


@dataclasses.dataclass
class Spec:
    qty_step: float = 0.001
    min_qty: float = 0.001
    min_notional: float = 6.0
    tick: float = 0.1


def _floor_to(x: float, step: float) -> float:
    dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.floor(x / step + 1e-9) * step, dp)


def _ceil_to(x: float, step: float) -> float:
    dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(math.ceil(x / step - 1e-9) * step, dp)


def size_position(budget: float, leverage: int, entry: float, sl: float, spec: Spec,
                  risk_pct: float = RISK_PCT, max_risk_pct: float = MAX_RISK_PCT) -> tuple[float, float, str]:
    """(quantity, risk_usdt, reason_if_zero) for a stop-defined trade."""
    per_unit = abs(entry - sl)
    if per_unit <= 0 or entry <= 0 or budget <= 0:
        return 0.0, 0.0, "invalid inputs"
    qty = _floor_to(budget * risk_pct / 100 / per_unit, spec.qty_step)
    min_q = max(spec.min_qty, _ceil_to(spec.min_notional / entry, spec.qty_step))
    if qty < min_q:
        if min_q * per_unit <= budget * max_risk_pct / 100:
            qty = min_q
        else:
            return 0.0, 0.0, (f"minimum size {min_q:g} would risk ${min_q * per_unit:.2f} "
                              f"(> {max_risk_pct:g}% of ${budget:.2f} budget)")
    max_q = _floor_to(budget * leverage / entry, spec.qty_step)
    qty = min(qty, max_q)
    if qty < min_q:
        return 0.0, 0.0, f"budget ${budget:.2f} × {leverage}x can't cover the minimum size {min_q:g}"
    return qty, qty * per_unit, ""


def round_levels(setup: Setup, tick: float) -> Setup:
    """Exchange ticks: entry to nearest, stop rounded away from entry, target toward it."""
    long_ = setup.direction == "long"
    entry = round(round(setup.entry / tick) * tick, 8)
    sl = _floor_to(setup.sl, tick) if long_ else _ceil_to(setup.sl, tick)
    tp = _floor_to(setup.tp, tick) if long_ else _ceil_to(setup.tp, tick)
    return dataclasses.replace(setup, entry=entry, sl=sl, tp=tp)


def _setup_from_json(s: str) -> Setup:
    return Setup(**json.loads(s))


def _ny_date(t_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(t_ms / 1000, NY).date()


def _day_start_ts(t_ms: int) -> int:
    d = dt.datetime.fromtimestamp(t_ms / 1000, dt.timezone.utc).astimezone(NY).date()
    return ny_time_to_ms(d, 0) // 1000


class ICTRunner:
    def __init__(self, db, trigger: dict) -> None:
        self.db = db
        self.trigger = trigger
        self.tid = trigger["id"]
        self.user_id = trigger.get("user_id")
        self.symbol = trigger["symbol"].upper()
        self.reference = reference_for(self.symbol)
        self.mode = (trigger.get("strategy_mode") or "paper").lower()
        self.leverage = int(trigger.get("leverage") or 1)
        self.budget = float(trigger.get("trade_amount_usdt") or 0.0)
        self.allow_shorts = bool(trigger.get("allow_shorts", 1))
        self.cfg = ICTConfig()
        self.engine = ICTEngine(self.cfg)
        self.costs = SimCosts()
        self.spec = Spec(tick=0.1 if self.symbol == "BTCUSDT" else 0.01)
        self.active: Optional[dict] = None        # DB row of the pending/open trade
        self.sim: Optional[SimTrade] = None       # paper mode only
        self._last_bar: Optional[Bar] = None

    # ── plumbing ──────────────────────────────────────────────────────────────

    async def _telegram(self):
        from database import queries
        from exchange.auto_trade import _get_user_telegram
        settings = await queries.get_user_settings(self.db, self.user_id) if self.user_id else None
        return await _get_user_telegram(settings or {})

    async def _client_call(self, fn_name: str, *args, **kwargs):
        """Run one CoinDCX call with this user's keys."""
        import aiohttp
        from auth.encryption import safe_decrypt
        from database import queries
        from exchange.coindcx_client import CoinDCXClient
        settings = await queries.get_user_settings(self.db, self.user_id) or {}
        key = safe_decrypt(settings.get("coindcx_api_key_enc"))
        secret = safe_decrypt(settings.get("coindcx_api_secret_enc"))
        if not key or not secret:
            raise RuntimeError("no CoinDCX API keys configured")
        async with aiohttp.ClientSession() as session:
            client = CoinDCXClient(api_key=key, api_secret=secret, session=session)
            return await getattr(client, fn_name)(*args, **kwargs)

    async def load_spec(self) -> None:
        try:
            import aiohttp
            from exchange.coindcx_client import CoinDCXClient
            async with aiohttp.ClientSession() as session:
                i = await CoinDCXClient("", "", session).get_futures_instrument(self.symbol)
            self.spec = Spec(
                qty_step=float(i.get("quantity_increment") or self.spec.qty_step),
                min_qty=float(i.get("min_trade_size") or i.get("quantity_increment") or self.spec.min_qty),
                min_notional=float(i.get("min_notional") or self.spec.min_notional),
                tick=float(i.get("price_increment") or self.spec.tick),
            )
        except Exception as exc:
            _log.warning("ICT #%d: instrument spec unavailable (%s) — using defaults %s", self.tid, exc, self.spec)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def warmup(self, prim: list[Bar], ref: list[Bar]) -> None:
        """Replay history through the engine (no orders), then restore today's state."""
        from database import queries
        rmap = {b.t: b for b in ref}
        pairs = [(p, rmap[p.t]) for p in prim if p.t in rmap]
        for p, r in pairs:
            for e in self.engine.on_minute(p, r):
                if e.kind == "setup":
                    self.engine.order_finished(False)   # historical — never traded
        if pairs:
            self._last_bar = pairs[-1][0]
            n = await queries.count_strategy_fills_since(self.db, self.tid, _day_start_ts(pairs[-1][0].t))
            if n:
                self.engine.mark_traded_today(n)
        self.active = await queries.get_active_strategy_trade(self.db, self.tid)
        if self.active:
            self.engine.hold()
            if self.active["mode"] == "paper":
                self._restore_paper(pairs)
                if pairs:
                    await self._sync_paper(pairs[-1][0])   # it may have filled/closed while we were down
        journal("ict_ready", trigger_id=self.tid, symbol=self.symbol, reference=self.reference,
                mode=self.mode, bars=len(pairs), active_trade=(self.active or {}).get("id"))

    def _restore_paper(self, pairs) -> None:
        """Re-simulate a paper trade over the bars missed while the bot was down."""
        row = self.active
        setup = _setup_from_json(row["setup_json"])
        self.sim = SimTrade(setup, self.spec.tick, ny_time_to_ms(_ny_date(setup.created_ms), _hm(self.cfg.time_stop)))
        for p, _ in pairs:
            if p.t >= setup.created_ms and self.sim.state in ("pending", "open"):
                step(self.sim, p, self.costs)

    async def on_minute(self, p: Bar, r: Bar) -> None:
        self._last_bar = p
        if self.active:
            await (self._manage_paper(p) if self.active["mode"] == "paper" else self._manage_live(p))
        for e in self.engine.on_minute(p, r):
            await self._handle_event(e)

    # ── engine events ─────────────────────────────────────────────────────────

    async def _handle_event(self, e) -> None:
        tg_bot, chat = await self._telegram()
        if e.kind == "plan":
            journal("ict_plan", trigger_id=self.tid, symbol=self.symbol, bias=e.info.get("bias"),
                    htf=e.info.get("htf"), reason=e.info.get("reason"),
                    levels={k: v for k, v in e.info.get("levels", {}).get("p", {}).items()})
            if tg_bot:
                from telegram_bot.alerts import send_ict_plan
                await send_ict_plan(tg_bot, chat, self.tid, self.symbol, self.reference, e.info, self.mode)
        elif e.kind == "no_trade":
            journal("ict_no_trade", trigger_id=self.tid, symbol=self.symbol, reason=e.info.get("reason"))
        elif e.kind == "setup":
            await self._open(e.setup, tg_bot, chat)

    async def _open(self, setup: Setup, tg_bot, chat) -> None:
        from database import queries
        from telegram_bot.alerts import send_ict_setup

        def skip(reason: str) -> None:
            journal("ict_setup_skipped", trigger_id=self.tid, symbol=self.symbol, side=setup.direction,
                    reason=reason, entry=setup.entry, sl=setup.sl, tp=setup.tp)
            self.engine.order_finished(False)

        if setup.direction == "short" and not self.allow_shorts:
            return skip("shorts disabled for this trigger")
        setup = round_levels(setup, self.spec.tick)
        qty, risk_usdt, why = size_position(self.budget, self.leverage, setup.entry, setup.sl, self.spec)
        if qty <= 0:
            return skip(why)

        order_id = None
        if self.mode == "live":
            try:
                live = await self._client_call("get_active_futures_position", self.symbol)
                if live is not None:
                    return skip(f"exchange already has a {self.symbol} position — one position per pair")
                others = await queries.get_open_futures_positions_on_symbol(self.db, self.symbol)
                if others:
                    return skip(f"bot trigger(s) {[o['trigger_id'] for o in others]} hold {self.symbol}")
                res = await self._client_call(
                    "create_futures_order", side="buy" if setup.direction == "long" else "sell",
                    pair=self.symbol, order_type="limit_order", quantity=qty, leverage=self.leverage,
                    price=setup.entry, sl_price=setup.sl, tp_price=setup.tp)
                from exchange.futures_trade import _order_id_from_result
                order_id = _order_id_from_result(res)
            except Exception as exc:
                journal("ict_order_failed", trigger_id=self.tid, symbol=self.symbol, error=str(exc)[:300])
                if tg_bot:
                    from telegram_bot.alerts import send_ict_update
                    await send_ict_update(tg_bot, chat, self.tid, self.symbol, "error", self.mode,
                                          f"Limit order rejected: {str(exc)[:200]}")
                self.engine.order_finished(False)
                return

        row_id = await queries.insert_strategy_trade(
            self.db, trigger_id=self.tid, user_id=self.user_id, strategy="ict_smt", mode=self.mode,
            symbol=self.symbol, side=setup.direction, entry_price=setup.entry, sl_price=setup.sl,
            tp_price=setup.tp, quantity=qty, leverage=self.leverage, risk_usdt=round(risk_usdt, 4),
            status="pending", cdx_order_id=order_id, setup_json=json.dumps(dataclasses.asdict(setup)),
            created_at=setup.created_ms // 1000, expires_at=setup.expiry_ms // 1000)
        self.active = await queries.get_active_strategy_trade(self.db, self.tid)
        if self.mode == "paper":
            self.sim = SimTrade(setup, self.spec.tick, ny_time_to_ms(_ny_date(setup.created_ms), _hm(self.cfg.time_stop)))
        journal("ict_setup", trigger_id=self.tid, symbol=self.symbol, mode=self.mode, row=row_id,
                side=setup.direction, entry=setup.entry, sl=setup.sl, tp=setup.tp, rr=round(setup.rr, 2),
                qty=qty, risk_usdt=round(risk_usdt, 4), order_id=order_id, reasons=setup.reasons)
        if tg_bot:
            await send_ict_setup(tg_bot, chat, self.tid, self.symbol, setup, qty, risk_usdt, self.mode)

    # ── paper ─────────────────────────────────────────────────────────────────

    async def _manage_paper(self, bar: Bar) -> None:
        if self.sim is None:
            self._restore_paper([])
        if step(self.sim, bar, self.costs) is not None:
            await self._sync_paper(bar)

    async def _sync_paper(self, bar: Bar) -> None:
        """Write the simulated trade's state transitions to the DB and Telegram."""
        from database import queries
        row, qty = self.active, self.active["quantity"]
        if self.sim.fill_ms and row.get("filled_at") is None:
            await queries.update_strategy_trade(self.db, row["id"], status="open", filled_at=self.sim.fill_ms // 1000)
            row["filled_at"] = self.sim.fill_ms // 1000
            self.engine.order_finished(True)
            await self._notify("filled", f"Paper fill at {self.sim.setup.entry:,.2f}")
        if self.sim.state == "cancelled":
            await queries.update_strategy_trade(self.db, row["id"], status="cancelled",
                                                exit_reason=self.sim.exit_reason, closed_at=bar.t // 1000)
            self.engine.order_finished(False)
            await self._notify("cancelled", self.sim.exit_reason)
            self.active, self.sim = None, None
        elif self.sim.state == "closed":
            pnl = self.sim.pnl_per_unit(self.costs) * qty
            fees = (self.sim.setup.entry * self.costs.maker + self.sim.exit_price * self.costs.taker) * qty
            r = self.sim.r_net(self.costs)
            await queries.update_strategy_trade(
                self.db, row["id"], status="closed", closed_at=(self.sim.exit_ms or bar.t) // 1000,
                exit_price=self.sim.exit_price,
                exit_reason=self.sim.exit_reason, pnl_usdt=round(pnl, 4), fees_usdt=round(fees, 4),
                r_multiple=round(r, 3))
            await self._notify("closed", f"{self.sim.exit_reason} at {self.sim.exit_price:,.2f} → "
                                         f"{r:+.2f}R (${pnl:+.4f} on {qty:g})")
            self.active, self.sim = None, None

    # ── live ──────────────────────────────────────────────────────────────────

    async def _manage_live(self, bar: Bar) -> None:
        from database import queries
        row = self.active
        long_ = row["side"] == "long"
        now = int(time.time())
        try:
            live = await self._client_call("get_active_futures_position", self.symbol)
        except Exception as exc:
            journal("ict_poll_failed", trigger_id=self.tid, error=str(exc)[:200])
            return
        mine = live is not None and (float(live.get("active_pos") or 0) > 0) == long_

        if row["status"] == "pending":
            if mine:
                await self._on_live_fill(row, live)
                return
            reached_tp = bar.h >= row["tp_price"] if long_ else bar.l <= row["tp_price"]
            if now >= (row["expires_at"] or 0) or reached_tp:
                reason = "target reached before the retrace" if reached_tp else "expired unfilled"
                try:
                    if row.get("cdx_order_id"):
                        await self._client_call("cancel_futures_order", row["cdx_order_id"])
                except Exception as exc:
                    journal("ict_cancel_failed", trigger_id=self.tid, error=str(exc)[:200])
                live = await self._client_call("get_active_futures_position", self.symbol)
                if live is not None and (float(live.get("active_pos") or 0) > 0) == long_:
                    await self._on_live_fill(row, live)      # filled while we were cancelling
                    return
                await queries.update_strategy_trade(self.db, row["id"], status="cancelled",
                                                    exit_reason=reason, closed_at=now)
                self.engine.order_finished(False)
                await self._notify("cancelled", reason)
                self.active = None
            return

        # open
        if not mine:
            await self._record_live_exit(row)
            return
        if now * 1000 + 60000 >= self._time_stop_ms(row):
            try:
                await self._client_call("exit_futures_position", str(live["id"]))
                await queries.update_strategy_trade(self.db, row["id"], exit_reason="time stop")
                row["exit_reason"] = "time stop"
                journal("ict_time_stop", trigger_id=self.tid, symbol=self.symbol)
            except Exception as exc:
                journal("ict_time_stop_failed", trigger_id=self.tid, error=str(exc)[:200])

    def _time_stop_ms(self, row: dict) -> int:
        setup = _setup_from_json(row["setup_json"])
        return ny_time_to_ms(_ny_date(setup.created_ms), _hm(self.cfg.time_stop))

    async def _on_live_fill(self, row: dict, live: dict) -> None:
        from database import queries
        qty = abs(float(live.get("active_pos") or 0))
        avg = float(live.get("avg_price") or row["entry_price"])
        await queries.update_strategy_trade(self.db, row["id"], status="open", filled_at=int(time.time()),
                                            position_id=str(live.get("id")), quantity=qty, entry_price=avg)
        row.update(status="open", filled_at=int(time.time()), position_id=str(live.get("id")),
                   quantity=qty, entry_price=avg)
        self.engine.order_finished(True)
        if qty + 1e-12 < float(row.get("quantity") or qty) and row.get("cdx_order_id"):
            try:
                await self._client_call("cancel_futures_order", row["cdx_order_id"])   # drop the unfilled rest
            except Exception:
                pass
        protected = float(live.get("stop_loss_trigger") or 0) > 0
        if not protected:
            try:
                await self._client_call("create_futures_tpsl", str(live["id"]),
                                        sl_price=row["sl_price"], tp_price=row["tp_price"])
                protected = True
            except Exception as exc:
                journal("ict_tpsl_failed", trigger_id=self.tid, error=str(exc)[:300])
                try:
                    await self._client_call("exit_futures_position", str(live["id"]))
                    await self._notify("error", "Could not attach a stop-loss — position closed immediately.")
                except Exception as exc2:
                    await self._notify("error", f"🚨 NO STOP-LOSS and exit failed: {str(exc2)[:150]}. "
                                                "Close it in the CoinDCX app NOW.")
                return
        journal("ict_filled", trigger_id=self.tid, symbol=self.symbol, qty=qty, avg=avg, protected=protected)
        await self._notify("filled", f"Filled {qty:g} @ {avg:,.2f} · SL {row['sl_price']:,.2f} · "
                                     f"TP {row['tp_price']:,.2f} (exchange-side)")

    async def _record_live_exit(self, row: dict) -> None:
        """Position is gone — read what the exchange booked since the fill."""
        from database import queries
        from exchange.coindcx_client import CoinDCXClient
        pair = CoinDCXClient._futures_pair(self.symbol)
        since_ms = (row.get("filled_at") or row["created_at"]) * 1000 - 5000
        realised = fees = funding = 0.0
        tpsl = False
        try:
            rows = await self._client_call("get_futures_transactions", page=1, size=100)
            for t in rows:
                if t.get("pair") != pair or float(t.get("created_at") or 0) < since_ms:
                    continue
                stage = (t.get("stage") or "").lower()
                amt, fee = float(t.get("amount") or 0), float(t.get("fee_amount") or 0)
                fees += fee
                if stage == "funding":
                    funding += amt
                else:
                    realised += amt
                    tpsl = tpsl or stage == "tpsl_exit"
        except Exception as exc:
            journal("ict_exit_lookup_failed", trigger_id=self.tid, error=str(exc)[:200])
        qty, entry = row["quantity"], row["entry_price"]
        long_ = row["side"] == "long"
        exit_px = entry + (realised / qty if long_ else -realised / qty) if qty else entry
        net = realised - fees + funding
        reason = row.get("exit_reason") or ("take profit" if tpsl and realised > 0 else
                                            "stop loss" if tpsl else "closed outside the bot")
        risk = abs(entry - row["sl_price"]) * qty
        r = net / risk if risk else 0.0
        await queries.update_strategy_trade(self.db, row["id"], status="closed", closed_at=int(time.time()),
                                            exit_price=round(exit_px, 8), exit_reason=reason,
                                            pnl_usdt=round(net, 4), fees_usdt=round(fees, 4), r_multiple=round(r, 3))
        journal("ict_closed", trigger_id=self.tid, symbol=self.symbol, reason=reason, pnl=round(net, 4),
                r=round(r, 3), fees=round(fees, 4), funding=round(funding, 4))
        await self._notify("closed", f"{reason}: ≈{exit_px:,.2f} → {r:+.2f}R (${net:+.4f} after fees)")
        self.active = None

    async def _notify(self, event: str, detail: str) -> None:
        tg_bot, chat = await self._telegram()
        if tg_bot:
            from telegram_bot.alerts import send_ict_update
            await send_ict_update(tg_bot, chat, self.tid, self.symbol, event, self.mode, detail)


def _hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def _to_bar(k) -> Bar:
    return Bar(k.open_time, k.open, k.high, k.low, k.close, k.volume)


async def run_ict_trigger(db, trigger: dict) -> None:
    """Poll closed 1m futures candles for the pair forever and drive the runner."""
    from streaming.stream import fetch_futures_klines_range
    runner = ICTRunner(db, trigger)
    await runner.load_spec()
    now = int(time.time() * 1000)
    start = now - WARMUP_DAYS * 86_400_000
    prim, ref = await asyncio.gather(
        fetch_futures_klines_range(runner.symbol, "1m", start, now),
        fetch_futures_klines_range(runner.reference, "1m", start, now))
    await runner.warmup([_to_bar(k) for k in prim], [_to_bar(k) for k in ref])
    last_t = runner._last_bar.t if runner._last_bar else now - 120_000
    _log.info("ICT #%d %s (ref %s) ready in %s mode — %d warm-up bars",
              runner.tid, runner.symbol, runner.reference, runner.mode, len(prim))
    while True:
        await asyncio.sleep(60 - (time.time() % 60) + 3)       # 3 s after each minute closes
        now = int(time.time() * 1000)
        try:
            p, r = await asyncio.gather(
                fetch_futures_klines_range(runner.symbol, "1m", last_t + 60_000, now),
                fetch_futures_klines_range(runner.reference, "1m", last_t + 60_000, now))
        except Exception as exc:
            _log.warning("ICT #%d candle fetch failed: %s", runner.tid, exc)
            continue
        rmap = {k.open_time: k for k in r}
        for k in p:
            if k.open_time in rmap and k.open_time > last_t:
                try:
                    await runner.on_minute(_to_bar(k), _to_bar(rmap[k.open_time]))
                except Exception as exc:
                    import traceback
                    _log.error("ICT #%d minute %d failed: %s\n%s", runner.tid, k.open_time, exc,
                               traceback.format_exc())
                last_t = k.open_time
