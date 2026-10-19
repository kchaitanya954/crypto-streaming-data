"""
CoinDCX fee rates as actually charged (verified against account fills, Oct 2026).

Spot USDT markets charged 0.2% per side on every one of 936 fills — the code
previously assumed 0.1%, which halved the fee drag in every P&L estimate.
Futures rates include 18% GST (instrument endpoint: maker 0.0236, taker 0.059).
"""

SPOT_FEE_RATE          = 0.002     # 0.20% per side
FUTURES_TAKER_FEE_RATE = 0.00059   # 0.059% per side (market orders, SL/TP triggers)
FUTURES_MAKER_FEE_RATE = 0.000236  # 0.0236% per side (resting limit orders)
