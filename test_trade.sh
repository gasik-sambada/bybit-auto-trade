#!/usr/bin/env bash
# test_trade.sh — Test the bybit-auto-trade service endpoints
#
# Usage:
#   ./test_trade.sh                    # defaults to localhost:9000
#   ./test_trade.sh http://1.2.3.4:9000
#
set -euo pipefail

BASE_URL="${1:-http://localhost:9000}"
SECRET="${AUTO_TRADE_SECRET:-}"  # Set env var or leave empty if no auth

# Colour helpers
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()    { echo -e "${YELLOW}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }
fail()    { echo -e "${RED}[FAIL]${NC} $*"; }

auth_header() {
  if [[ -n "$SECRET" ]]; then
    echo "-H \"X-Auto-Trade-Secret: $SECRET\""
  fi
}

# Build curl auth args array
CURL_AUTH=()
if [[ -n "$SECRET" ]]; then
  CURL_AUTH=(-H "X-Auto-Trade-Secret: $SECRET")
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Bybit Auto-Trade Service — Test Suite"
echo "  Target: $BASE_URL"
echo "════════════════════════════════════════════════════════"

# ── 1. Ping ───────────────────────────────────────────────────────────────────
info "1. GET /ping"
RESP=$(curl -sf "$BASE_URL/ping" 2>&1) && success "pong: $RESP" || fail "ping failed: $RESP"
echo ""

# ── 2. Health ─────────────────────────────────────────────────────────────────
info "2. GET /health"
RESP=$(curl -sf "$BASE_URL/health" 2>&1)
if echo "$RESP" | grep -q '"status"'; then
  success "health ok"
  echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
else
  fail "health failed: $RESP"
fi
echo ""

# ── 3. Open trade — Market order, BTCUSDT ─────────────────────────────────────
info "3. POST /trade — open Market order (BTCUSDT BUY)"
PAYLOAD='{
  "action":     "open",
  "symbol":     "BTCUSDT",
  "side":       "BUY",
  "price":      "105000",
  "sl":         "104000",
  "tp":         "108000",
  "order_type": "Market"
}'
RESP=$(curl -sf -X POST "$BASE_URL/trade" \
  -H "Content-Type: application/json" \
  "${CURL_AUTH[@]}" \
  -d "$PAYLOAD" 2>&1)
if echo "$RESP" | grep -q '"status"'; then
  echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
  echo "$RESP" | grep -q '"succeeded"' && success "open trade sent" || fail "unexpected response"
else
  fail "open trade failed: $RESP"
fi
echo ""

# ── 4. Open trade — Limit order, ETHUSDT ─────────────────────────────────────
info "4. POST /trade — open Limit order (ETHUSDT SELL)"
PAYLOAD='{
  "action":     "open",
  "symbol":     "ETHUSDT",
  "side":       "SELL",
  "price":      "3500",
  "sl":         "3600",
  "tp":         "3200",
  "order_type": "Limit"
}'
RESP=$(curl -sf -X POST "$BASE_URL/trade" \
  -H "Content-Type: application/json" \
  "${CURL_AUTH[@]}" \
  -d "$PAYLOAD" 2>&1)
if echo "$RESP" | grep -q '"status"'; then
  echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
else
  fail "limit trade failed: $RESP"
fi
echo ""

# ── 5. Close all — BTCUSDT ───────────────────────────────────────────────────
info "5. POST /close — close_all (BTCUSDT)"
PAYLOAD='{
  "action": "close_all",
  "symbol": "BTCUSDT"
}'
RESP=$(curl -sf -X POST "$BASE_URL/close" \
  -H "Content-Type: application/json" \
  "${CURL_AUTH[@]}" \
  -d "$PAYLOAD" 2>&1)
if echo "$RESP" | grep -q '"status"'; then
  echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
  success "close request sent"
else
  fail "close failed: $RESP"
fi
echo ""

# ── 6. Unknown symbol ─────────────────────────────────────────────────────────
info "6. POST /trade — unknown symbol (should return error)"
PAYLOAD='{
  "action": "open",
  "symbol": "UNKNOWNXYZ",
  "side":   "BUY",
  "price":  "100",
  "sl":     "90",
  "tp":     "120"
}'
RESP=$(curl -sf -X POST "$BASE_URL/trade" \
  -H "Content-Type: application/json" \
  "${CURL_AUTH[@]}" \
  -d "$PAYLOAD" 2>&1)
echo "$RESP" | python3 -m json.tool 2>/dev/null || echo "$RESP"
echo "$RESP" | grep -q "not configured" && success "correctly rejected unknown symbol" || info "check response above"
echo ""

echo "════════════════════════════════════════════════════════"
echo "  Tests complete."
echo "  NOTE: Actual Bybit orders require valid API keys."
echo "        Use testnet (PROD_ENV=false) to test safely."
echo "════════════════════════════════════════════════════════"
