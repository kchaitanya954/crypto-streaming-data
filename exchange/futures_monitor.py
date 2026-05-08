"""
Futures position monitor — trailing stop and breakeven stop.

Called every closed candle (from orchestrator._stream_pair) for each symbol.
Updates sl_price in the DB; the existing SL/TP software-enforcement in
orchestrator._check_sl_tp will then close the position when the new SL is hit.

Trailing stop logic:
  LONG:  new_sl = max(current_sl, current_price * (1 - TRAIL_PCT))
  SHORT: new_sl = min(current_sl, current_price * (1 + TRAIL_PCT))
  Trailing only starts once price has moved TRAIL_ACTIVATION_PCT in our favour.

Breakeven stop logic:
  Once price reaches 50% of the distance to TP, move SL to entry + BREAKEVEN_BUFFER.
  This locks in a small profit and removes risk of a full SL loss.
"""

import logging

_log = logging.getLogger("futures_monitor")

TRAIL_PCT             = 0.006   # trailing gap: SL trails 0.6% behind current price
TRAIL_ACTIVATION_PCT  = 0.010   # only start trailing after 1.0% favorable move
BREAKEVEN_TRIGGER     = 0.50    # move SL to breakeven when 50% of TP distance reached
BREAKEVEN_BUFFER      = 0.0005  # lock in 0.05% above entry for LONG breakeven SL


async def monitor_futures_positions(
    db,
    symbol: str,
    current_price: float,
) -> None:
    """
    For each open futures position on this symbol, apply trailing stop and
    breakeven stop logic, updating sl_price in the DB if it improves.
    """
    from database import queries

    positions = await queries.get_open_futures_positions_for_sl_tp(db, symbol)
    for pos in positions:
        side    = pos["side"]
        entry   = pos["entry_price"]
        sl      = pos["sl_price"]
        tp      = pos["tp_price"]
        pos_id  = pos["id"]

        if side == "long":
            favorable_move = (current_price - entry) / entry
            new_sl = sl

            # Breakeven: once price reaches 50% of TP target, floor SL at entry+buffer
            tp_dist = tp - entry
            if tp_dist > 0 and current_price >= entry + BREAKEVEN_TRIGGER * tp_dist:
                be_sl = round(entry * (1.0 + BREAKEVEN_BUFFER), 8)
                new_sl = max(new_sl, be_sl)

            # Trailing: only activate after 1% favorable move, then trail at TRAIL_PCT
            if favorable_move >= TRAIL_ACTIVATION_PCT:
                trail_sl = round(current_price * (1.0 - TRAIL_PCT), 8)
                new_sl = max(new_sl, trail_sl)

            if new_sl > sl + 1e-8:
                _log.info(
                    "Trailing SL: pos %d LONG %s  SL %.4f → %.4f  (price=%.4f, entry=%.4f)",
                    pos_id, symbol, sl, new_sl, current_price, entry,
                )
                await queries.update_futures_sl_price(db, pos_id, new_sl)

        else:  # short
            favorable_move = (entry - current_price) / entry
            new_sl = sl

            # Breakeven
            tp_dist = entry - tp
            if tp_dist > 0 and current_price <= entry - BREAKEVEN_TRIGGER * tp_dist:
                be_sl = round(entry * (1.0 - BREAKEVEN_BUFFER), 8)
                new_sl = min(new_sl, be_sl)

            # Trailing
            if favorable_move >= TRAIL_ACTIVATION_PCT:
                trail_sl = round(current_price * (1.0 + TRAIL_PCT), 8)
                new_sl = min(new_sl, trail_sl)

            if new_sl < sl - 1e-8:
                _log.info(
                    "Trailing SL: pos %d SHORT %s  SL %.4f → %.4f  (price=%.4f, entry=%.4f)",
                    pos_id, symbol, sl, new_sl, current_price, entry,
                )
                await queries.update_futures_sl_price(db, pos_id, new_sl)
