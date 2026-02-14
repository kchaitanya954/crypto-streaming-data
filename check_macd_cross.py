#!/usr/bin/env python3
"""
Stream ETHUSDT closed candles and print when MACD line crosses above or below the signal line.

Run from repo root:
  python check_macd_cross.py
  python check_macd_cross.py --interval 5m
"""

import argparse
import asyncio
from datetime import datetime

from streaming.stream import stream_klines, VALID_INTERVALS
from indicators import macd


# MACD params (standard 12, 26, 9)
FAST, SLOW, SIGNAL = 12, 26, 9
# First bar index where both MACD and signal are defined
MIN_BARS = SLOW - 1 + SIGNAL  # 34


def _ts(open_time_ms: int) -> str:
    return datetime.utcfromtimestamp(open_time_ms / 1000).strftime("%Y-%m-%d %H:%M:%S UTC")


async def run(
    symbol: str = "ethusdt",
    interval: str = "1m",
) -> None:
    closes: list[float] = []
    open_times: list[int] = []  # open_time per bar, same length as closes

    print(f"Streaming {symbol.upper()} {interval} (closed candles). Need {MIN_BARS + 1} bars before MACD cross detection.\n")

    async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=True):
        if not kline.is_closed:
            continue
        closes.append(kline.close)
        open_times.append(kline.open_time)

        if len(closes) < MIN_BARS + 1:
            # Need current and previous bar with MACD/signal to detect cross
            if len(closes) == MIN_BARS:
                print(f"Warming up: {len(closes)} bars. Watching for MACD crossovers next bar.\n")
            continue

        res = macd(closes, fast_period=FAST, slow_period=SLOW, signal_period=SIGNAL)
        n = len(closes)
        macd_prev = res.macd_line[n - 2]
        macd_curr = res.macd_line[n - 1]
        sig_prev = res.signal_line[n - 2]
        sig_curr = res.signal_line[n - 1]

        if macd_prev is None or macd_curr is None or sig_prev is None or sig_curr is None:
            continue

        # Cross above: was below or equal, now above
        if macd_prev <= sig_prev and macd_curr > sig_curr:
            t = _ts(open_times[-1])
            print(f"[{t}] MACD crossed ABOVE signal  |  close={kline.close:.2f}  macd={macd_curr:.4f}  signal={sig_curr:.4f}", flush=True)

        # Cross below: was above or equal, now below
        if macd_prev >= sig_prev and macd_curr < sig_curr:
            t = _ts(open_times[-1])
            print(f"[{t}] MACD crossed BELOW signal  |  close={kline.close:.2f}  macd={macd_curr:.4f}  signal={sig_curr:.4f}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Print MACD crossovers for ETHUSDT.")
    parser.add_argument("--symbol", default="ethusdt", help="Trading pair (default: ethusdt)")
    parser.add_argument("--interval", default="1m", choices=VALID_INTERVALS, help="Candle interval (default: 1m)")
    args = parser.parse_args()

    try:
        asyncio.run(run(symbol=args.symbol, interval=args.interval))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
