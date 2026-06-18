# bybit-auto-trade

A lightweight Python/FastAPI service that receives trade signals and executes orders on Bybit for **multiple accounts** simultaneously.

Designed to run on a server with direct Bybit API access, receiving commands from [`telegram-alert-signal`](https://github.com/gasik-sambada/telegram-alert-signal) (or any HTTP client).

---

## Architecture

```
TradingView → [telegram-alert-signal] ──Telegram──► Users
                         │
                  (if symbol enabled)
                         │
                         ▼
             [bybit-auto-trade]  ← this repo
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
         Account 1              Account 2 ...
         (Bybit API)            (Bybit API)
```

---

## Features

- ✅ **Multi-account** — trade the same signal across multiple Bybit accounts simultaneously
- ✅ **FUTURES & SPOT** — per-symbol market type, auto-detected from `.P` suffix
- ✅ **Risk-based qty** — qty calculated from `min_loss_usd` + SL distance + leverage
- ✅ **Set leverage** — automatically sets leverage before each FUTURES order
- ✅ **Market & Limit orders** — default per symbol, overridable per alert
- ✅ **Update SL/TP** — update stop-loss and take-profit on open positions
- ✅ **Close all** — cancel all orders + close all positions for a symbol
- ✅ **Shared secret** — optional service-to-service auth

---

## Quantity Formula

```
FUTURES:  qty = min_loss_usd / |entry_price - sl_price| / leverage
SPOT:     qty = min_loss_usd / |entry_price - sl_price|
```

**Example:** `min_loss = $10`, `entry = 105000`, `sl = 104000`, `leverage = 10x`
```
qty = 10 / 1000 / 10 = 0.001 BTC
```

---

## Setup

### 1. Configure symbols

Edit [`config/symbols.yaml`](config/symbols.yaml):

```yaml
symbols:
  BTCUSDT.P:       # .P = perpetual futures (auto-detected as FUTURES)
    leverage: 10
    order_type: Market   # default; overridable per-alert

  ETHUSDT.P:
    leverage: 10
    order_type: Market

  SOLUSDT.P:
    leverage: 10
    order_type: Market

  # BTCUSDT:        # no .P = SPOT (auto-detected)
  #   order_type: Market
```

> **Symbol naming:** `.P` suffix mirrors TradingView's perpetual contract naming.
> When calling the Bybit API, `.P` is stripped automatically (the `category` param distinguishes FUTURES vs SPOT).

### 2. Configure accounts

Copy and edit the env file:

```bash
cp .env.example .env
nano .env
```

```env
AUTO_TRADE_SECRET=my-internal-secret
PORT=9000
LOG_LEVEL=INFO

# Account 1
ACCOUNT_1_NAME=Alice
ACCOUNT_1_API_KEY=your-api-key
ACCOUNT_1_API_SECRET=your-api-secret
ACCOUNT_1_PROD_ENV=false           # true = Mainnet, false = Testnet
ACCOUNT_1_SYMBOLS=BTCUSDT.P,ETHUSDT.P
ACCOUNT_1_MIN_LOSS_USD=10

# Optional: per-symbol leverage override (overrides symbols.yaml)
# ACCOUNT_1_BTCUSDT.P_LEVERAGE=20

# Account 2
ACCOUNT_2_NAME=Bob
ACCOUNT_2_API_KEY=your-api-key
ACCOUNT_2_API_SECRET=your-api-secret
ACCOUNT_2_PROD_ENV=false
ACCOUNT_2_SYMBOLS=BTCUSDT.P,SOLUSDT.P
ACCOUNT_2_MIN_LOSS_USD=5
```

> Add more accounts by incrementing the number: `ACCOUNT_3_*`, `ACCOUNT_4_*`, etc.

### 3. Deploy with Docker

```bash
docker compose up -d
```

The `config/` directory is mounted as a volume — you can edit `symbols.yaml` without rebuilding the image.

---

## API Endpoints

| Method | Endpoint  | Description |
|--------|-----------|-------------|
| GET    | `/health` | Returns all accounts, symbols, and settings |
| GET    | `/ping`   | Simple liveness check |
| POST   | `/trade`  | Open position, update SL/TP, or close all |
| POST   | `/close`  | Cancel orders + close all positions |

All `POST` endpoints require the `X-Auto-Trade-Secret` header if `AUTO_TRADE_SECRET` is set.

---

## Testing with curl

Replace `localhost:9000` with your server address and `your-secret-here` with your `AUTO_TRADE_SECRET`.

### 📋 Health check

```bash
curl http://localhost:9000/health | python3 -m json.tool
```

---

### 🟢 Open Trade — Market order (default)

```bash
curl -X POST http://localhost:9000/trade \
  -H "Content-Type: application/json" \
  -H "X-Auto-Trade-Secret: your-secret-here" \
  -d '{
    "action": "open",
    "symbol": "BTCUSDT.P",
    "side":   "BUY",
    "price":  "105000",
    "sl":     "104000",
    "tp":     "108000"
  }'
```

### 🟢 Open Trade — Limit order (override per-alert)

```bash
curl -X POST http://localhost:9000/trade \
  -H "Content-Type: application/json" \
  -H "X-Auto-Trade-Secret: your-secret-here" \
  -d '{
    "action":     "open",
    "symbol":     "BTCUSDT.P",
    "side":       "SELL",
    "price":      "105000",
    "sl":         "106000",
    "tp":         "102000",
    "order_type": "Limit"
  }'
```

---

### 🔄 Update SL/TP — both values

```bash
curl -X POST http://localhost:9000/trade \
  -H "Content-Type: application/json" \
  -H "X-Auto-Trade-Secret: your-secret-here" \
  -d '{
    "action": "update_sl_tp",
    "symbol": "BTCUSDT.P",
    "sl":     "104500",
    "tp":     "109000"
  }'
```

### 🔄 Update SL only (keep existing TP)

```bash
curl -X POST http://localhost:9000/trade \
  -H "Content-Type: application/json" \
  -H "X-Auto-Trade-Secret: your-secret-here" \
  -d '{
    "action": "update_sl_tp",
    "symbol": "BTCUSDT.P",
    "sl":     "104800"
  }'
```

---

### 🔴 Close All — cancel orders + close positions

```bash
curl -X POST http://localhost:9000/close \
  -H "Content-Type: application/json" \
  -H "X-Auto-Trade-Secret: your-secret-here" \
  -d '{
    "action": "close_all",
    "symbol": "BTCUSDT.P"
  }'
```

> **Note:** `close_all` can also be sent to `/trade` — both endpoints accept it.

---

### 🧪 Run the test script

```bash
# Against local dev server
AUTO_TRADE_SECRET=your-secret ./test_trade.sh

# Against remote server
AUTO_TRADE_SECRET=your-secret ./test_trade.sh http://your-server:9000
```

---

## Supported Actions

| Action         | Endpoint | Description |
|----------------|----------|-------------|
| `open`         | `/trade` | Open a new position (sets leverage, calculates qty, places order) |
| `update_sl_tp` | `/trade` | Update SL/TP on all open positions for the symbol |
| `close_all`    | `/trade` or `/close` | Cancel all orders + close all open positions |

---

## Integration with telegram-alert-signal

Set these env vars in your `telegram-alert-signal` service:

```env
AUTO_TRADE_URL=http://bybit-auto-trade:9000
AUTO_TRADE_SECRET=my-internal-secret
AUTO_TRADE_SYMBOLS=["BTCUSDT.P","ETHUSDT.P","SOLUSDT.P"]
```

TradingView alert JSON fields forwarded automatically:
- `action` — `open`, `close_all`, `update_sl_tp`
- `symbol` — exchange prefix stripped, `.P` preserved
- `side`, `price`, `sl`, `tp` — passed through as-is
- `order_type` — optional; overrides per-symbol default in `symbols.yaml`

---

## Order Type Priority

```
TradingView alert "order_type" field
        ↓ (if absent)
symbols.yaml per-symbol default
        ↓ (if not set)
Market (hardcoded fallback)
```

---

## Notes

- Use `ACCOUNT_N_PROD_ENV=false` + testnet API keys to test safely before going live
- Leverage is set automatically before each FUTURES order (error 110043 = already set, safely ignored)
- `update_sl_tp` is **FUTURES only** — SPOT positions don't support SL/TP via this API
- `symbols.yaml` is mounted as a Docker volume — edit it without rebuilding the container
