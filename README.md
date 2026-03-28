# Crypto Signal Dashboard

Real-time crypto trading signal dashboard with technical indicator analysis, Telegram alerts, and optional CoinDCX order execution.

Market data is sourced from **Binance** (public WebSocket — no API key needed).
Order execution uses **CoinDCX** (requires API keys).

---

## Project layout

```
crypto-streaming-data/
├── indicators/          # Technical indicator calculations (EMA, MACD, RSI, Stochastic, OBV, ADX)
├── signals/             # Signal detector — trend gates + confluence logic
│   └── detector.py      # SignalDetector, Signal, IndicatorSnapshot
├── streaming/           # Binance WebSocket stream + REST history fetch
├── exchange/            # CoinDCX authenticated REST client
├── database/            # SQLite — candles, signals, orders
├── telegram_bot/        # Telegram bot — alerts + manual trade commands
├── ui/
│   ├── server.py        # FastAPI server + WebSocket streaming endpoint
│   └── static/          # Dashboard HTML/JS (TradingView Lightweight Charts)
├── orchestrator.py      # Full-stack launcher (DB + Exchange + Telegram + Dashboard)
├── run_ui.py            # Dashboard-only launcher (no Telegram, no DB)
├── check_macd_cross.py  # CLI signal printer (terminal only)
└── .env.example         # Environment variable template
```

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in your credentials
```

---

## Running

### 1. Dashboard only (no credentials needed)

Charts, indicators, and live signal detection with no external accounts required.

```bash
python run_ui.py
python run_ui.py --symbol ethusdt --interval 5m
python run_ui.py --port 8080
```

Open **http://localhost:8000** in your browser.

- Use the interval buttons (1s / 1m / 5m / 15m …) to switch timeframes
- Type a symbol (e.g. `ETHUSDT`) in the box and press **Enter** to switch pairs
- BUY/SELL signals appear in the left sidebar with confidence level and reasons

---

### 2. Full stack — Dashboard + Telegram + CoinDCX

Requires API keys in `.env`. See [Credentials setup](#credentials-setup) below.

```bash
python orchestrator.py
```

This starts:
- SQLite database (`data/trading.db`)
- CoinDCX client (portfolio display + order execution)
- Telegram bot (signal alerts + `/buy` `/sell` commands)
- Dashboard at **http://localhost:8000**

---

## Credentials setup

Edit your `.env` file:

### CoinDCX (portfolio + order execution)
1. Go to **coindcx.com → Settings → API**
2. Create a new API key with **"Create Order"** and **"Get Balance"** permissions
3. Add to `.env`:
   ```
   COINDCX_API_KEY=your_key
   COINDCX_API_SECRET=your_secret
   ```

### Telegram bot
1. Open Telegram → search **@BotFather** → send `/newbot`
2. Follow the prompts — copy the bot token it gives you
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_token
   ```
4. **Get your Chat ID:**
   - Search **@userinfobot** on Telegram and send it any message
   - It will reply with your numeric chat ID
   - Add to `.env`:
     ```
     TELEGRAM_CHAT_ID=your_chat_id
     ```
5. Start a conversation with your new bot (search it by name, click **Start**)

---

## Telegram bot commands

Once `orchestrator.py` is running and you've started a chat with your bot:

| Command | Description |
|---|---|
| `/start` | Show all available commands |
| `/status` | CoinDCX balances + last 3 signals |
| `/buy MARKET QTY PRICE` | Place a buy order with confirmation |
| `/sell MARKET QTY PRICE` | Place a sell order with confirmation |
| `/history` | Last 10 trade fills |

**Examples:**
```
/buy BTCUSDT 0.001 67000       # limit buy
/sell ETHUSDT 0.01 market      # market sell
/buy BTCUSDT 0.001             # market buy (omit price)
```

Signal alerts are sent automatically whenever a BUY or SELL signal fires.

---

## Indicators used

| Indicator | Role |
|---|---|
| EMA(200) | Long-term trend gate — blocks counter-trend signals |
| EMA(50) | Medium-term trend gate |
| ADX(14) | Trend strength — skips signals in choppy/ranging markets (threshold: 30) |
| MACD(12,26,9) | Crossover trigger — requires minimum histogram gap to avoid micro-crosses |
| RSI(14) | Momentum confirmation |
| Stochastic(14,3) | Entry timing confirmation |
| OBV | Volume confirmation |
| Bollinger Bands(20,2) | Volatility overlay on chart |

**Signal confidence:**
- `HIGH` — 3 confirmations (RSI + Stochastic + OBV all agree)
- `MEDIUM` — 2 confirmations
- `LOW` — 1 confirmation (suppressed by default — `min_confirmations=2`)

---

## Trading phases (`.env` → `TRADING_PHASE`)

| Phase | Behaviour |
|---|---|
| `1` (Manual) | Alerts only — you trade manually via dashboard or Telegram `/buy` `/sell` |
| `2` (Semi-auto) | HIGH confidence signals send a Telegram approval request before executing |
| `3` (Full-auto) | All HIGH confidence signals execute automatically on CoinDCX |

---

## CLI signal printer (no UI)

```bash
python check_macd_cross.py
python check_macd_cross.py --symbol btcusdt --interval 5m
```

Prints signals directly to terminal with full indicator breakdown.
