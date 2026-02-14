#!/usr/bin/env python3
"""
Stream crypto OHLCV data from Binance for analysis (e.g. MACD, RSI).

Usage:
  python stream_crypto.py                    # BTC/USDT 1m, print to stdout
  python stream_crypto.py --symbol ethusdt --interval 5m
  python stream_crypto.py --closed --out data/btc_1m.csv   # save closed candles to CSV
"""

from streaming.cli import main

if __name__ == "__main__":
    main()
