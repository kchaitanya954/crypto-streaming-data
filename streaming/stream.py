"""
Binance WebSocket kline (candlestick) stream for crypto OHLCV data.
No API key required for public market data.
"""

import json
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

import websockets


BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"
VALID_INTERVALS = ("1s","1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M")


@dataclass
class Kline:
    """Single candlestick (OHLCV) from the stream."""

    symbol: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool
    trade_count: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "is_closed": self.is_closed,
            "trade_count": self.trade_count,
        }


def _parse_kline(raw: dict) -> Kline:
    k = raw["k"]
    return Kline(
        symbol=k["s"],
        interval=k["i"],
        open_time=int(k["t"]),
        close_time=int(k["T"]),
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        is_closed=bool(k["x"]),
        trade_count=int(k["n"]),
    )


def _build_stream_url(symbol: str, interval: str) -> str:
    symbol = symbol.lower().strip()
    if interval not in VALID_INTERVALS:
        raise ValueError(f"interval must be one of {VALID_INTERVALS}, got {interval!r}")
    return f"{BINANCE_WS_BASE}/{symbol}@kline_{interval}"


async def stream_klines(
    symbol: str = "btcusdt",
    interval: str = "1m",
    *,
    only_closed: bool = False,
    on_kline: Optional[Callable[[Kline], None]] = None,
) -> AsyncIterator[Kline]:
    """
    Stream kline (candlestick) data from Binance.

    Args:
        symbol: Trading pair (e.g. btcusdt, ethusdt).
        interval: Candle interval (1m, 5m, 15m, 1h, etc.).
        only_closed: If True, yield only closed candles (one per interval).
        on_kline: Optional callback called for each kline (closed or not).

    Yields:
        Kline objects. If only_closed=True, only closed candles; otherwise every update.
    """
    url = _build_stream_url(symbol, interval)

    async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
        async for message in ws:
            data = json.loads(message)
            if data.get("e") != "kline":
                continue
            kline = _parse_kline(data)
            if on_kline:
                on_kline(kline)
            if only_closed and not kline.is_closed:
                continue
            yield kline


async def run_stream(
    symbol: str = "btcusdt",
    interval: str = "1m",
    only_closed: bool = False,
    on_kline: Optional[Callable[[Kline], None]] = None,
) -> None:
    """
    Run the kline stream indefinitely (for use as main entrypoint).
    """
    url = _build_stream_url(symbol, interval)
    print(f"Connecting to {url} (only_closed={only_closed})")
    async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
        async for message in ws:
            data = json.loads(message)
            if data.get("e") != "kline":
                continue
            kline = _parse_kline(data)
            if on_kline:
                on_kline(kline)
            if only_closed and not kline.is_closed:
                continue
            if on_kline is None:
                print(kline, flush=True)
