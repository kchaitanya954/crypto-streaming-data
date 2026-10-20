# Crypto Signal Dashboard & Auto-Trader

Real-time crypto trading signal dashboard with technical indicator analysis, automated CoinDCX order execution, Telegram alerts, adaptive position sizing, and a full analytics suite.

Market data is sourced from **Binance** (public WebSocket — no API key needed).  
Order execution uses **CoinDCX** (requires API keys stored encrypted in the DB).

---

## Project layout

```
crypto-streaming-data/
├── indicators/
│   └── indicators.py        # EMA, MACD, RSI, Stochastic, OBV, ADX, Bollinger Bands
├── signals/
│   ├── detector.py          # SignalDetector — trend gates + confluence logic → Signal
│   ├── adaptive_strategy.py # Half-Kelly position sizing engine (AdaptiveState)
│   ├── ict_smt.py           # ICT/SMT NY-open engine (bias, levels, sweep, SMT, FVG → Setup)
│   └── ict_sim.py           # Conservative fill/exit simulator (backtest + paper mode)
├── backtest/
│   └── ict_backtest.py      # 2-year ICT/SMT backtest on Binance futures 1m data
├── streaming/
│   └── stream.py            # Binance WebSocket stream + REST candle history fetch
├── exchange/
│   ├── coindcx_client.py    # CoinDCX authenticated REST client (orders, balances, ledgers)
│   ├── auto_trade.py        # Auto-trade executor — BUY/SELL sizing, SL/TP, fee math
│   ├── futures_trade.py     # Futures executor + entry guards
│   ├── ict_runner.py        # ICT/SMT trigger runner (paper / live)
│   ├── account_ledger.py    # Exchange-ledger summaries for the daily report
│   └── fees.py              # CoinDCX fee rates as actually charged
├── database/
│   ├── db.py                # SQLite schema (candles, signals, orders, triggers, …)
│   └── queries.py           # All DB read/write helpers
├── telegram_bot/
│   ├── alerts.py            # Signal alerts, trade confirmations, daily P&L report
│   ├── bot.py               # Telegram bot entry point
│   ├── handlers.py          # /start /status /history command handlers
│   └── keyboards.py         # Inline keyboard builders
├── auth/
│   ├── security.py          # JWT token generation + validation
│   └── encryption.py        # Fernet encryption for stored API keys
├── ui/
│   ├── server.py            # FastAPI server — REST API + WebSocket streaming
│   └── static/
│       ├── index.html       # Dashboard UI (TradingView Lightweight Charts)
│       └── app.js           # All frontend logic (signals, triggers, analytics, …)
├── orchestrator.py          # Full-stack launcher (DB + Exchange + Telegram + Dashboard)
├── log_setup.py             # logs/bot.log + logs/decisions.jsonl
├── run_ui.py                # Dashboard-only launcher (no Telegram, no auto-trade)
├── check_macd_cross.py      # CLI signal printer (terminal only, no UI)
├── docker-compose.yml       # Docker deployment
├── Dockerfile
└── .env.example             # Environment variable template
```

---

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then fill in your credentials
```

Generate the required secret keys:

```bash
# JWT secret (authentication tokens)
python -c "import secrets; print(secrets.token_hex(32))"

# Fernet key (encrypts stored CoinDCX API keys)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Add both outputs to your `.env` as `JWT_SECRET` and `FERNET_KEY`.

> **Warning:** Never change `FERNET_KEY` after it's been used — it makes all stored exchange keys unreadable.

---

## Running

### 1. Dashboard only (no credentials needed)

Charts, live indicators, and signal detection — no exchange account required.

```bash
python run_ui.py
python run_ui.py --symbol ethusdt --interval 5m
python run_ui.py --port 8080
```

Open **http://localhost:8000** in your browser.

---

### 2. Full stack — Dashboard + Telegram + Auto-Trading

Requires API keys configured via the dashboard Settings page (stored encrypted in the DB).

```bash
python orchestrator.py
```

This starts:
- SQLite database at `data/trading.db`
- CoinDCX client (portfolio + auto order execution)
- Telegram bot (signal alerts + trade confirmations + daily P&L reports)
- Dashboard at **http://localhost:8000**
- Background signal worker (runs 24/7 watching all active triggers)
- Adaptive engine monitor (updates Kelly sizing every 5 min from real trades)
- Daily P&L scheduler (fires at 10:00 AM IST = 04:30 UTC)

---

### 3. Docker

```bash
docker-compose up --build
```

---

## Credentials setup

CoinDCX API keys and Telegram credentials are configured **through the dashboard UI** (Settings page), not via `.env`. They are stored encrypted in the SQLite database using Fernet symmetric encryption.

The `.env` file only needs:

```env
JWT_SECRET=<64-char random hex>
FERNET_KEY=<Fernet key from cryptography library>
DB_PATH=data/trading.db
PORT=8000
```

**CoinDCX API key:**
1. Go to [coindcx.com → Settings → API](https://coindcx.com/settings/api)
2. Create a key with **"Create Order"** and **"Get Balance"** permissions
3. Enter the key + secret in the dashboard → Settings

**Telegram bot:**
1. Message **@BotFather** → `/newbot` → copy the token
2. Get your chat ID: message **@userinfobot** on Telegram
3. Enter both in the dashboard → Settings

---

## How auto-trading works

### Triggers

A **trigger** is a saved watch rule: `(symbol, interval, min_confidence, amount_usdt)`.

- Example: _Watch ETHUSDT on 1m, fire on MEDIUM+ signals, budget $10_
- Each trigger has an isolated budget — it only uses the USDT amount you assigned to it
- The bot runs all active triggers simultaneously in the background

### Signal → Order flow

```
Binance WebSocket → closed candle → indicators → signal detector
       ↓
Signal fires (BUY or SELL, with confidence: HIGH / MEDIUM / LOW)
       ↓
Matches active triggers for that symbol+interval with enough confidence
       ↓
auto_trade.py: compute position size → place market order on CoinDCX
       ↓
Record order in DB → send Telegram confirmation → update adaptive engine
```

### BUY sizing (adaptive, per-trigger)

Each BUY deploys a **percentage of the trigger's remaining budget**:

| Confidence | Base % of remaining budget |
|---|---|
| HIGH | 70% |
| MEDIUM | 50% |
| LOW | 30% |

This percentage is then scaled by:
- **Half-Kelly multiplier** (0.5× – 1.5×) — based on rolling win rate + avg win/loss
- **ADX strength** — stronger trend → deploy more (up to 1.2×)
- **Performance multiplier** — recent avg P&L influences size (0.25× – 2.0×)
- **Drawdown guard** — if portfolio down ≥5%/10%/20% → reduce size

Result is clamped to `[10%, 90%]` of remaining budget.

> **Example:** $10 trigger, MEDIUM signal, no drawdown → deploy 50% of $10 = $5 first trade.  
> Next MEDIUM signal → 50% of remaining $5 = $2.50. Geometric decay protects the budget.

### SELL sizing (confidence-based)

| Confidence | Fraction of trigger's coins sold |
|---|---|
| HIGH | 100% |
| MEDIUM | 60% |
| LOW | 30% |

Sell ratio increases by 10–20% during drawdown or consecutive losses (exit more aggressively when down).

### Trigger budget isolation

- BUY uses only the trigger's `remaining budget = trigger_amount − usdt_spent`
- SELL uses only the coins that **this specific trigger** purchased (tracked in `trigger_positions` table)
- The bot never touches coins bought by a different trigger or manually

### Stop-loss / Take-profit

After every BUY, the background monitor watches the price and fires a market SELL if:
- Price drops **2% below avg entry** → stop-loss
- Price rises **4% above avg entry** (+ fee buffer) → take-profit

Both thresholds are fee-adjusted: `avg_entry` = signal price × 1.001 (includes buy-side fee), so SL/TP are computed against actual cost, not the raw signal price.

### Fee accounting

Rates as actually charged on this account (verified against every fill, Oct 2026 — see `exchange/fees.py`):

| Market | Per side | Round trip |
|---|---|---|
| Spot USDT pairs | **0.20%** | 0.40% |
| Futures, market / SL / TP (taker, incl. 18% GST) | 0.059% | 0.118% |
| Futures, resting limit (maker, incl. GST) | 0.0236% | — |

A spot trade must move **more than 0.4%** just to break even — the 1m tier's 0.4% stop equals the fee.
Earlier versions assumed 0.1% spot / 0.05% futures, which understated fees in every report.

---

## Adaptive position sizing (Half-Kelly)

The `AdaptiveState` engine learns from every real executed trade and continuously updates sizing:

**Kelly fraction:**
```
f* = (W × b − L) / b     where b = avg_win / avg_loss
Half-Kelly = f* / 2       capped at 15%
```

**Circuit breaker** (does NOT stop trading — only reduces size):
- 3+ consecutive losses → size reduced by up to 0.6×
- 5+ consecutive losses → size reduced by up to 0.3×
- Portfolio drawdown ≥10% → size further reduced

The engine persists its state to the DB and restores on restart. It resets to conservative defaults if no real trades exist yet.

---

## Trading phases

Set via `TRADING_PHASE` in `.env` (or the dashboard):

| Phase | Behaviour |
|---|---|
| `1` Manual | Signal alerts only — you trade manually via dashboard or Telegram commands |
| `2` Semi-auto | HIGH confidence signals send a Telegram approval button before executing |
| `3` Full-auto | Signals execute automatically on CoinDCX based on trigger rules |

---

## Analytics dashboard

Open the **Analytics** tab in the dashboard to see:

### Real Trade P&L (executed orders)
- All actual BUY/SELL orders grouped into cycles per (symbol, trigger)
- Fee-adjusted P&L: cost includes +0.1% buy fee, proceeds net −0.1% sell fee
- **Cost/Coin w/fee** — true cost per coin including buy fee
- **Break-even price** — minimum sell price needed to profit after round-trip fees
- Highlights cycles where sell price was below break-even (loss from fees)

### Signal Analytics
- Historical signal performance (paired BUY→SELL signals)
- Win rate, total/avg P&L by symbol and by confidence level

### Portfolio Simulation
- Walk-forward backtest using the adaptive Half-Kelly engine on historical signals

### Adaptive Engine State
- Live Kelly fraction, win rate, performance multiplier, drawdown %, circuit-breaker status
- Recommended position sizes for HIGH/MEDIUM/LOW signals

### Daily P&L
- Per-trigger P&L for any calendar day (IST timezone)
- Fees paid, gross buy/sell, fee-adjusted net P&L

---

## Daily Telegram P&L report

The bot sends a report to every user at **10:00 AM IST (04:30 UTC)** automatically.

- **Exchange section first (source of truth):** read from CoinDCX's own ledgers
  (`exchange/account_ledger.py`) — futures realised P&L, fees and funding per pair
  (USDT and INR books), liquidations, spot fills/fees with FIFO P&L, open positions
  (flagged ⚠️ when they have **no stop-loss**) and futures-wallet balances. This
  includes exchange-side SL/TP fills, manual trades and accidental positions that the
  bot's own tables never see. If the bot's records disagree with the exchange by more
  than $0.10 the report says so.
- ICT/SMT strategy trades (paper and live) for the window
- Grouped by trigger (symbol + interval) — the bot's own records
- Shows: buy count, sell count, gross amounts, fees paid, fee-adjusted net P&L
- Grand total row across all triggers
- If the service starts after 04:30 UTC, it sends a **catch-up report immediately** for that day, then resumes normal scheduling
- Telegram send failures are retried once (30s delay) and logged with full traceback

---

## Logs and decision journal

`log_setup.py` writes, in addition to the console:

- `logs/bot.log` — everything at INFO+, rotated 10 MB × 10
- `logs/decisions.jsonl` — one JSON line per decision: every signal, every trade taken or
  **skipped with its reason**, orders, fills, closes, ICT plans/setups, unprotected-position alerts

```bash
tail -f logs/decisions.jsonl                         # live
grep trade_skipped logs/decisions.jsonl | tail -20   # why didn't it trade?
```

Docker mounts `./logs` so the files survive rebuilds. Set `LOG_DIR` to move them.

---

## Futures safety guards

- **One position per pair.** CoinDCX nets every order on a pair into one position, so
  two triggers trading the same pair in opposite directions cancel out and pay fees both
  ways. A trigger won't open if another trigger (or anything untracked — a manual trade,
  a leftover) already holds the pair on the exchange.
- **Exit signals close only.** The detector's position-exit signals skip its EMA/ADX entry
  gates, so they may close a position but never open the opposite one.
- **Shorts switch** per trigger (`allow_shorts`, "Shorts" checkbox).
- **Position watchdog** (every 5 min): Telegram alert for any live futures position without
  an exchange stop-loss. Set `FUTURES_AUTO_PROTECT_PCT=3` to also attach a 3% stop
  automatically (off by default because it also touches manual positions).
- Account audit: `python -m scripts.coindcx_audit --days 30` (read-only).

---

## Daily trend strategy (spot) and the recommended trigger set

`signals/trend.py` + `exchange/trend_runner.py`: three breakout systems (20/10, 50/25,
100/50 days) share a spot trigger's budget, one third each. At every daily close
(00:00 UTC = 05:30 IST) a flat system buys on a close above its prior N-day high and a
long system sells on a close below its prior M-day low. Entries only on a fresh
breakout; each system sells only the coins it bought. Paper or live like ICT.

Why this and not the indicator triggers (`python -m backtest.trigger_backtest`, real
detector + the bot's own exits, CoinDCX fees, 2020–22 vs 2023–26):

| Strategy (BTC) | 2020–22 | 2023–26 | Max DD 2023–26 |
|---|---|---|---|
| Indicator trigger 1d, spot | −6% | −20% | −36% |
| Indicator trigger 4h, futures long | +33% | −26% | −28% |
| **Daily trend, spot (3-system blend)** | **+227%** | **+101%** | **−26%** |
| Buy & hold | +146% | +396% | −53% |

Trend was positive in both periods at every length from 20 to 200 days on BTC. On ETH
it was weak since 2023 (+15%), and on futures (1x) funding made it worse every time.
It is a drawdown cutter, not a guarantee — it trails buy-and-hold in strong bull years.

Create the set on the server (dry run first):

```bash
python -m scripts.create_triggers --user <name>                              # shows the plan
python -m scripts.create_triggers --user <name> --apply --pause-indicator   # creates, pauses MACD triggers
```

| Trigger | Market | Mode | Budget (default) |
|---|---|---|---|
| BTC trend · daily | spot | **LIVE** | $45 (needs spot USDT) |
| ETH trend · daily | spot | paper | $30 |
| BTC ICT/SMT · NY open (shorts off) | futures 5x | paper | $17 |

---

## ICT / SMT strategy (NY open)

A mechanical version of the "NY open" routine, adapted to crypto: CoinDCX has no S&P 500 /
Nasdaq futures (`B-SPX_USDT` is the SPX6900 memecoin), so SMT is measured between **BTC and ETH**.

| Routine step | Implementation (`signals/ict_smt.py`) |
|---|---|
| Before 09:30 NY, prepare | Bias + session levels frozen at 09:30 New York (19:00 IST in EDT, 20:00 IST in EST) |
| 1h/4h: will it rise or fall? | 1h market structure (break of structure on fractal swings) |
| 1m/5m: precise setup | 5m bars built from 1m futures candles |
| SMT divergence | One asset sweeps an untaken session level (Asia / London / pre-market / previous day), the other doesn't |
| Fair value gap | Displacement candle (body ≥ 1×ATR) leaving a 3-bar FVG and breaking structure |
| Enter on the retrace | Resting limit at the FVG midpoint (maker fee), expires after 30 min |
| SL 1R, TP 2–3R at liquidity | SL beyond the sweep extreme; TP = nearest untaken opposing liquidity ≥ 2R (cap 4R) |
| Exit / manage | Exchange SL/TP; time stop 15:55 NY; one filled trade per day; 09:30–11:00 NY only |

Create a **Futures** trigger and pick *Strategy → ICT / SMT · NY open*. Mode defaults to
**Paper** (simulated fills on live candles, same simulator as the backtest, results in
Telegram and the daily report). *Live* places real limit orders with exchange SL/TP, checks
the stop is attached after the fill, and closes the position if it can't attach one.
Sizing risks `ICT_RISK_PCT` (default 1%) of the trigger budget, up to `ICT_MAX_RISK_PCT`
(default 3%) when the exchange minimum size forces it.

### Backtest — read this before going live

```bash
python -m backtest.ict_backtest                    # BTC traded, ETH reference, 2y of 1m futures data
python -m backtest.ict_backtest --no-smt           # same setup without SMT
python -m backtest.ict_backtest --ltf 1 --csv trades.csv
```

Result on Oct 2024 → Oct 2026 Binance futures data, CoinDCX fees, conservative fills:
**22 trades, −0.27R per trade; out-of-sample −0.60R.** Out-of-sample was negative in all nine
variants tried (1m chart, ETH traded, 4h bias, edge entry, 3R targets, no SMT…). Shorts lost
in every variant. The routine does not have a demonstrated edge on BTC/ETH after costs —
keep it in Paper unless its own paper record says otherwise.

---

## Indicators used

| Indicator | Role |
|---|---|
| EMA(200) | Long-term trend gate — blocks counter-trend signals |
| EMA(50) | Medium-term trend gate |
| ADX(14) | Trend strength gate — skips signals when market is choppy (ADX < 20 threshold) |
| MACD(12,26,9) | Crossover trigger — requires minimum histogram gap to filter micro-crosses |
| Bollinger Bands(20,2) | BUY only below middle band, SELL only above it (room to move) |
| RSI(14) | Momentum confirmation |
| Stochastic(14,3) | Entry timing confirmation |
| OBV | Volume confirmation |

**Signal confidence:**
- `HIGH` — 3 confirmations (RSI + Stochastic + OBV all agree)
- `MEDIUM` — 2 confirmations
- `LOW` — 1 confirmation

---

## Telegram bot commands

| Command | Description |
|---|---|
| `/start` | Show all available commands |
| `/status` | CoinDCX balances + last 3 signals |
| `/history` | Last 10 trade fills |

Signal alerts and trade confirmations are sent automatically. Daily P&L arrives at 10 AM IST.

---

## Max concurrent positions

The bot limits open positions to **5 per user** across all triggers. New BUYs are blocked when this limit is reached to prevent over-exposure. The limit is set by `MAX_OPEN_POSITIONS` in `auto_trade.py`.

---

## Database tables

| Table | Contents |
|---|---|
| `users` | User accounts (username, email, hashed password) |
| `user_settings` | Per-user Telegram token/chat ID + encrypted CoinDCX keys |
| `candles` | OHLCV candle history per symbol+interval |
| `signals` | All detected signals with indicator snapshot |
| `triggers` | User-defined watch rules |
| `orders` | All placed orders with status |
| `trade_history` | Filled trade records with fee-adjusted P&L |
| `trigger_positions` | Per-trigger open position: coins held, avg entry, usdt spent, SL/TP |
| `adaptive_state` | Persisted Half-Kelly engine state |

---

## CLI signal printer (no UI)

```bash
python check_macd_cross.py
python check_macd_cross.py --symbol btcusdt --interval 5m
```

Prints signals directly to terminal with full indicator breakdown. No database or exchange required.
