"""
Tests for exchange/account_ledger.py and the exchange section of the daily report.
"""
import pytest

from exchange.account_ledger import (
    open_position_rows,
    summarize_futures_transactions,
    summarize_spot_fills,
)
from telegram_bot.alerts import format_exchange_section, send_daily_pnl_report

F, T = 1_000_000_000_000, 1_000_086_400_000          # a 24h window in ms


def _tx(stage, amount, fee, t, pair="B-BTC_USDT"):
    return {"pair": pair, "stage": stage, "amount": amount, "fee_amount": fee, "created_at": t}


def test_futures_ledger_counts_only_the_window():
    rows = [
        _tx("default", 0.0, 0.05, F + 1),             # open fill: fee only
        _tx("tpsl_exit", -1.0, 0.05, F + 10),          # stop hit on the exchange
        _tx("default", 2.0, 0.05, F + 20),             # bot market close, win
        _tx("funding", -0.01, 0.0, F + 30),
        _tx("liquidate", -3.0, 0.1, F + 40, "B-ETH_USDT"),
        _tx("default", 9.0, 0.05, T + 1),              # next day — excluded
    ]
    s = summarize_futures_transactions(rows, F, T)
    t = s["total"]
    assert t["pnl"] == pytest.approx(-2.0)
    assert t["fees"] == pytest.approx(0.25)
    assert t["funding"] == pytest.approx(-0.01)
    assert t["net"] == pytest.approx(-2.26)
    assert (t["closes"], t["wins"], t["losses"], t["liquidations"]) == (3, 1, 2, 1)
    assert s["pairs"]["B-ETH_USDT"]["liquidations"] == 1


def test_spot_fifo_uses_cost_basis_from_before_the_window():
    fills = [
        {"symbol": "ETHUSDT", "side": "buy", "quantity": "1", "price": "100", "fee_amount": "0.2", "timestamp": F - 5},
        {"symbol": "ETHUSDT", "side": "sell", "quantity": "1", "price": "110", "fee_amount": "0.22", "timestamp": F + 5},
        {"symbol": "ETHUSDT", "side": "sell", "quantity": "0.5", "price": "120", "fee_amount": "0.12", "timestamp": F + 6},
    ]
    s = summarize_spot_fills(fills, F, T)
    m = s["markets"]["ETHUSDT"]
    assert m["realised"] == pytest.approx(10.0)       # the 0.5 with no known cost isn't counted as profit
    assert m["unmatched_qty"] == pytest.approx(0.5)
    assert m["fees"] == pytest.approx(0.34)            # only in-window fills
    assert s["total"]["net"] == pytest.approx(10.0 - 0.34)


def test_open_positions_flag_missing_stop():
    rows = open_position_rows([
        {"pair": "B-BTC_USDT", "active_pos": 0.001, "avg_price": 80000, "mark_price": 81000,
         "stop_loss_trigger": 0.0, "take_profit_trigger": 0.0, "leverage": 5, "liquidation_price": 64000, "id": "p1"},
        {"pair": "B-ETH_USDT", "active_pos": 0.0, "avg_price": 0, "mark_price": 0},
    ])
    assert len(rows) == 1 and rows[0]["sl"] is None and rows[0]["upnl"] == pytest.approx(1.0)
    text = "\n".join(format_exchange_section({"open_positions": rows, "errors": []}))
    assert "NO STOP-LOSS" in text and "BTC" in text


@pytest.mark.asyncio
async def test_report_puts_exchange_first_and_flags_mismatch():
    class _Bot:
        text = None
        async def send_message(self, chat_id, text, parse_mode=None):
            _Bot.text = text

    ex = {"futures_usdt": summarize_futures_transactions([_tx("tpsl_exit", -1.0, 0.1, F + 10)], F, T),
          "open_positions": [], "wallets": {"USDT": 17.29}, "errors": []}
    rows = [{"market": "all", "trigger_id": None, "symbol": "ALL", "buy_count": 0, "sell_count": 0,
             "fee_usdt": 0.0, "true_pnl_usdt": 0.0, "net_pnl_usdt": 0.0, "spot_pnl_usdt": 0.0,
             "futures_pnl_usdt": 0.0}]
    await send_daily_pnl_report(_Bot(), "1", rows, "w", exchange=ex)
    assert _Bot.text.index("Exchange") < _Bot.text.index("Bot triggers")
    assert "$-1.1000" in _Bot.text                     # exchange net: −1 P&L − 0.1 fee
    assert "differ from the exchange" in _Bot.text
