"""
CLI for streaming crypto kline data from Binance.
"""

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from streaming.stream import Kline, run_stream, VALID_INTERVALS


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream crypto kline data from Binance (no API key required)."
    )
    parser.add_argument(
        "--symbol",
        default="btcusdt",
        help="Trading pair (e.g. btcusdt, ethusdt). Default: btcusdt",
    )
    parser.add_argument(
        "--interval",
        default="1m",
        choices=VALID_INTERVALS,
        help="Candle interval. Default: 1m",
    )
    parser.add_argument(
        "--closed",
        action="store_true",
        help="Emit only closed candles (one per interval); default is every update.",
    )
    parser.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Append closed candles to CSV file (implies --closed).",
    )
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        only_closed = True
        file_exists = out_path.exists()
        out_file = open(out_path, "a", newline="")
        writer = csv.DictWriter(
            out_file,
            fieldnames=[
                "symbol", "interval", "open_time", "close_time",
                "open", "high", "low", "close", "volume",
                "is_closed", "trade_count",
            ],
        )
        if not file_exists:
            writer.writeheader()

        def on_kline(k: Kline) -> None:
            if k.is_closed:
                writer.writerow(k.to_dict())
                out_file.flush()
    else:
        only_closed = args.closed
        on_kline = None

    try:
        asyncio.run(
            run_stream(
                symbol=args.symbol,
                interval=args.interval,
                only_closed=only_closed,
                on_kline=on_kline if out_path else None,
            )
        )
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    finally:
        if out_path:
            out_file.close()


if __name__ == "__main__":
    main()
