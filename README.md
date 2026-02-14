# crypto-streaming-data

Stream crypto OHLCV (candlestick) data from Binance and compute technical indicators (moving averages, MACD, RSI). Uses the public WebSocket API—no API key required.

## Project layout

- **`streaming/`** — Binance WebSocket stream and CLI
  - `stream.py` — kline stream client (`Kline`, `stream_klines`, `run_stream`)
  - `cli.py` — argument parsing and CSV output
- **`indicators/`** — Technical indicators
  - `indicators.py` — SMA, EMA, exponential MACD, RSI
- **`stream_crypto.py`** — Entrypoint to run the stream from the repo root

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

## Stream crypto data

**Live updates (every ~250 ms):**
```bash
python stream_crypto.py
python stream_crypto.py --symbol ethusdt --interval 5m
```

**Only closed candles (one per interval, for indicators):**
```bash
python stream_crypto.py --closed
```

**Save closed candles to CSV:**
```bash
python stream_crypto.py --out data/btc_1m.csv
```

**Options:** `--symbol` (default `btcusdt`), `--interval` (e.g. `1m`, `5m`, `1h`), `--closed`, `--out FILE`

## Indicators

Use closing prices (from the stream or CSV) with the `indicators` module:

```python
from indicators import sma, ema, macd, rsi, MACDResult

# prices = list of closes (oldest first), e.g. from stream or CSV
prices = [100.0, 102.0, 101.0, ...]

# Simple and exponential moving averages (period = 5)
sma5 = sma(prices, 5)   # None until 5 points, then SMA values
ema5 = ema(prices, 5)

# Exponential MACD (default 12, 26, 9)
result = macd(prices, fast_period=12, slow_period=26, signal_period=9)
# result.macd_line, result.signal_line, result.histogram

# RSI (default period 14)
rsi14 = rsi(prices, 14)
```

- **SMA** — Simple moving average: average of last `period` closes.
- **EMA** — Exponential moving average: multiplier `2 / (period + 1)`.
- **MACD** — MACD line = EMA(fast) − EMA(slow), Signal = EMA(MACD), Histogram = MACD − Signal.
- **RSI** — Relative Strength Index with Wilder’s smoothing (period 14).

All functions return lists aligned with the input; leading values where the indicator is undefined are `None`.
