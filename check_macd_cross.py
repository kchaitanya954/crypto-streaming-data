#!/usr/bin/env python3
"""
Stream closed candles and print buy/sell signals.

Indicators used:
  EMA(50) + EMA(200)  — trend direction gates
  ADX(14)             — trend strength gate (skips choppy markets)
  MACD(12,26,9)       — crossover trigger
  RSI(14)             — momentum confirmation
  Stochastic(14,3)    — entry timing confirmation
  OBV                 — volume confirmation

Run from repo root:
  python check_macd_cross.py
  python check_macd_cross.py --symbol btcusdt --interval 5m
"""

import argparse
import asyncio
from datetime import datetime

from streaming.stream import stream_klines, fetch_historical_klines, VALID_INTERVALS
from signals.detector import Signal, SignalDetector

# EMA(200) needs the most history
SEED_BARS = 201


def _ts(open_time_ms: int) -> str:
    return datetime.utcfromtimestamp(open_time_ms / 1000).strftime("%Y-%m-%d %H:%M:%S UTC")


def _print_signal(sig: Signal) -> None:
    print(
        f"[{_ts(sig.open_time)}] {sig.direction} [{sig.confidence}]"
        f"  entry_price={sig.entry_price:.2f}"
        f"  macd={sig.macd_val:.4f}  signal={sig.signal_val:.4f}  hist={sig.histogram:+.4f}",
        flush=True,
    )
    print(f"    Trend : {sig.trend_note}", flush=True)
    for r in sig.reasons:
        print(f"    + {r}", flush=True)
    print()


async def run(symbol: str = "ethusdt", interval: str = "1m") -> None:
    detector = SignalDetector()

    print(f"Fetching {SEED_BARS} historical bars for {symbol.upper()} {interval}...")
    historical = await fetch_historical_klines(symbol, interval, limit=SEED_BARS)
    detector.seed(historical)
    print(f"Seeded {len(historical)} bars. Watching for signals...\n")

    async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=True):
        if not kline.is_closed:
            continue
        sig = detector.update(kline)
        if sig:
            _print_signal(sig)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Buy/sell signals: EMA + ADX trend gates, MACD trigger, RSI/Stoch/OBV confirmations."
    )
    parser.add_argument("--symbol",   default="ethusdt", help="Trading pair (default: ethusdt)")
    parser.add_argument("--interval", default="1m", choices=VALID_INTERVALS, help="Candle interval (default: 1m)")
    args = parser.parse_args()

    try:
        asyncio.run(run(symbol=args.symbol, interval=args.interval))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
