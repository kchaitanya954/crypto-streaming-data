"""
FastAPI server for the crypto signal dashboard.

Each WebSocket connection gets its own independent stream + detector,
so multiple browser tabs can watch different symbols/intervals simultaneously.

Run from project root:
    python run_ui.py
    python run_ui.py --symbol btcusdt --interval 5m --port 8080
"""

import asyncio
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from signals.detector import SignalDetector
from streaming.stream import stream_klines, fetch_historical_klines

SEED_BARS  = 201
static_dir = Path(__file__).parent / "static"

app = FastAPI()
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    symbol:    str = Query(default="ethusdt"),
    interval:  str = Query(default="1m"),
):
    await websocket.accept()
    # Run the stream as a background task so we can cancel it on disconnect
    task = asyncio.create_task(
        _connection_loop(websocket, symbol.lower(), interval)
    )
    try:
        while True:
            await websocket.receive_text()   # keeps connection alive; raises on disconnect
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _connection_loop(ws: WebSocket, symbol: str, interval: str) -> None:
    """Fetch history, send it, then stream live candles + signals to one client."""
    detector = SignalDetector()
    try:
        # 1. Tell the client which symbol/interval we're streaming
        await ws.send_json({"type": "meta", "symbol": symbol.upper(), "interval": interval})

        # 2. Historical data
        historical = await fetch_historical_klines(symbol, interval, limit=SEED_BARS)
        detector.seed(historical)

        for snap in detector.history_snapshots():
            await ws.send_json({"type": "candle", **asdict(snap)})

        # 3. Signal to client that history is done — triggers scrollToRealTime()
        await ws.send_json({"type": "ready"})

        # 4. Live stream (both closed and in-progress candles)
        async for kline in stream_klines(symbol=symbol, interval=interval, only_closed=False):
            if kline.is_closed:
                signal = detector.update(kline)
                snap   = detector.current_snapshot()
                await ws.send_json({"type": "candle", **asdict(snap)})
                if signal:
                    await ws.send_json({
                        "type":        "signal",
                        "direction":   signal.direction,
                        "confidence":  signal.confidence,
                        "entry_price": signal.entry_price,
                        "time":        signal.open_time // 1000,
                        "macd_val":    signal.macd_val,
                        "signal_val":  signal.signal_val,
                        "histogram":   signal.histogram,
                        "adx_val":     signal.adx_val,
                        "reasons":     signal.reasons,
                        "trend_note":  signal.trend_note,
                    })
            else:
                # In-progress candle — update chart without recomputing indicators
                await ws.send_json({
                    "type":   "live",
                    "time":   kline.open_time // 1000,
                    "open":   kline.open,
                    "high":   kline.high,
                    "low":    kline.low,
                    "close":  kline.close,
                    "volume": kline.volume,
                })
    except Exception:
        pass   # WebSocketDisconnect or task cancelled
