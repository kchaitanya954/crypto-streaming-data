#!/usr/bin/env python3
"""Launch the crypto signal dashboard.

Usage:
    python run_ui.py                              # ETHUSDT 1m, port 8000
    python run_ui.py --symbol btcusdt --interval 5m
    python run_ui.py --port 8080
"""

import argparse
import os

import uvicorn

from streaming.stream import VALID_INTERVALS


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto signal dashboard.")
    parser.add_argument("--symbol",   default="ethusdt",
                        help="Trading pair (default: ethusdt)")
    parser.add_argument("--interval", default="1m", choices=VALID_INTERVALS,
                        help="Candle interval (default: 1m)")
    parser.add_argument("--port",     default=8000, type=int,
                        help="HTTP port (default: 8000)")
    args = parser.parse_args()

    os.environ["SYMBOL"]   = args.symbol.lower()
    os.environ["INTERVAL"] = args.interval

    print(f"Dashboard → http://localhost:{args.port}  ({args.symbol.upper()} {args.interval})")
    uvicorn.run("ui.server:app", host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
