"""
Binance WebSocket kline (candlestick) stream for crypto OHLCV data.
No API key required for public market data.
"""

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

import websockets


BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"
BINANCE_REST_BASE = "https://api.binance.com"
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


def _fetch_klines_sync(symbol: str, interval: str, limit: int) -> list[Kline]:
    url = (
        f"{BINANCE_REST_BASE}/api/v3/klines"
        f"?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        rows = json.loads(resp.read())
    return [
        Kline(
            symbol=symbol.upper(),
            interval=interval,
            open_time=int(r[0]),
            close_time=int(r[6]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
            is_closed=True,
            trade_count=int(r[8]),
        )
        for r in rows
    ]


async def fetch_historical_klines(symbol: str, interval: str, limit: int) -> list[Kline]:
    """Fetch the last `limit` closed klines from Binance REST API."""
    return await asyncio.to_thread(_fetch_klines_sync, symbol, interval, limit)


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


# ── USD-M futures (what CoinDCX "B-" pairs track) ─────────────────────────────

BINANCE_FUTURES_REST = "https://fapi.binance.com"


def _fetch_futures_klines_range_sync(symbol: str, interval: str, start_ms: int, end_ms: int,
                                     spot: bool = False) -> list[Kline]:
    """Closed klines in [start_ms, end_ms), paginated. USD-M futures by default, spot if spot=True."""
    out: list[Kline] = []
    cursor = start_ms
    base, path, cap = ((BINANCE_REST_BASE, "/api/v3/klines", 1000) if spot
                       else (BINANCE_FUTURES_REST, "/fapi/v1/klines", 1500))
    while cursor < end_ms:
        url = (f"{base}{path}?symbol={symbol.upper()}&interval={interval}"
               f"&startTime={cursor}&endTime={end_ms - 1}&limit={cap}")
        with urllib.request.urlopen(url, timeout=15) as resp:
            rows = json.loads(resp.read())
        if not rows:
            break
        for r in rows:
            out.append(Kline(symbol=symbol.upper(), interval=interval, open_time=int(r[0]),
                             close_time=int(r[6]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                             close=float(r[4]), volume=float(r[5]), is_closed=True, trade_count=int(r[8])))
        cursor = int(rows[-1][0]) + 1
        if len(rows) < cap:
            break
    now_ms = int(time.time() * 1000)
    return [k for k in out if k.close_time < now_ms]   # drop the still-forming candle


async def fetch_futures_klines_range(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Kline]:
    """
    Futures candles (the prices CoinDCX B- pairs follow); falls back to spot candles
    if the futures API is unreachable from this network.
    """
    try:
        return await asyncio.to_thread(_fetch_futures_klines_range_sync, symbol, interval, start_ms, end_ms)
    except Exception:
        return await asyncio.to_thread(_fetch_futures_klines_range_sync, symbol, interval, start_ms, end_ms, True)
