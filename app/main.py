"""FastAPI application for bybit-auto-trade service."""
from __future__ import annotations

import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, status

from .config import Config
from .trader import TradeRequest, execute_trade

# ── Config ────────────────────────────────────────────────────────────────────
# Resolve symbols.yaml path: prefer /app/config (container) then ./config (dev)
_YAML_PATHS = ["/app/config/symbols.yaml", "config/symbols.yaml"]
_yaml_path = next((p for p in _YAML_PATHS if Path(p).exists()), "config/symbols.yaml")
config = Config.load(symbols_yaml_path=_yaml_path)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bybit-auto-trade")


# ── App lifecycle ─────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Bybit Auto-Trade Service started")
    logger.info(f"   Accounts loaded:  {[a.name for a in config.accounts]}")
    logger.info(f"   Symbols in YAML:  {sorted(config.enabled_symbols)}")
    logger.info(f"   Secret auth:      {'✅ set' if config.auto_trade_secret else '⚠️  not set (open access)'}")
    yield
    logger.info("👋 Bybit Auto-Trade Service stopped")


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="Bybit Auto-Trade",
    version="1.0.0",
    description="Receives trade signals and executes orders on Bybit for multiple accounts.",
    lifespan=lifespan,
)


# ── Auth helper ───────────────────────────────────────────────────────────────
def _authorized(request: Request) -> bool:
    """Check X-Auto-Trade-Secret header if secret is configured."""
    if not config.auto_trade_secret:
        return True  # No secret configured — open access
    return request.headers.get("X-Auto-Trade-Secret", "") == config.auto_trade_secret


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/ping")
async def ping():
    """Simple liveness check."""
    return "pong"


@app.get("/health")
async def health():
    """Detailed health check — returns configured accounts and symbols."""
    return {
        "status": "ok",
        "accounts": [
            {
                "name":      a.name,
                "prod_env":  a.prod_env,
                "symbols":   sorted(a.symbols),
                "min_loss":  a.min_loss_usd,
            }
            for a in config.accounts
        ],
        "symbols": {
            sym: {
                "market_type": sc.market_type,
                "leverage":    sc.leverage,
                "order_type":  sc.order_type,
            }
            for sym, sc in config.symbols.items()
        },
    }


@app.post("/trade")
async def trade(request: Request):
    """
    Receive a trade command and execute it across all matching accounts.

    Supported actions:
      open         — Open a new position
      close_all    — Cancel all orders + close all positions
      update_sl_tp — Update SL/TP on all open positions for the symbol

    Expected JSON body for 'open':
        {
          "action":     "open",
          "symbol":     "BTCUSDT.P",
          "side":       "BUY",          // BUY or SELL
          "price":      "105000.00",    // entry price (required for Limit)
          "sl":         "104000.00",    // stop-loss price (required)
          "tp":         "108000.00",    // take-profit price (optional)
          "order_type": "Market"        // optional: overrides per-symbol default
        }

    Expected JSON body for 'update_sl_tp':
        {
          "action": "update_sl_tp",
          "symbol": "BTCUSDT.P",
          "sl":     "104500.00",    // new stop-loss  (optional, omit to keep current)
          "tp":     "109000.00"     // new take-profit (optional, omit to keep current)
        }

    Expected JSON body for 'close_all':
        {
          "action": "close_all",
          "symbol": "BTCUSDT.P"
        }
    """
    if not _authorized(request):
        logger.warning(f"Unauthorized request from {request.client.host}")
        return Response(content="Unauthorized", status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        data = await request.json()
    except Exception as exc:
        return {"status": "error", "error": f"Invalid JSON: {exc}"}

    action = data.get("action", "")
    symbol = data.get("symbol", "")

    if not symbol:
        return {"status": "error", "error": "symbol is required"}
    if action not in ("open", "close_all", "update_sl_tp"):
        return {"status": "error", "error": f"Unsupported action: {action!r} (expected open, close_all, or update_sl_tp)"}

    logger.info(f"📩 /trade: action={action} symbol={symbol} side={data.get('side', '')} "
                f"price={data.get('price', '')} sl={data.get('sl', '')} order_type={data.get('order_type', '(default)')}")

    req = TradeRequest(
        action=action,
        symbol=symbol,
        side=data.get("side", ""),
        price=str(data.get("price", "")),
        sl=str(data.get("sl", "")),
        tp=str(data.get("tp", "")),
        order_type=str(data.get("order_type", "")),
    )

    try:
        results = await execute_trade(config, req)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"💥 Unhandled exception in /trade:\n{tb}")
        return {"status": "error", "error": str(exc)}

    succeeded = [r for r in results if r.success]
    failed    = [r for r in results if not r.success]

    logger.info(f"✅ /trade done: {len(succeeded)} ok, {len(failed)} failed")

    return {
        "status":   "ok" if succeeded else "error",
        "symbol":   symbol,
        "action":   action,
        "succeeded": [
            {"account": r.account_name, "qty": r.qty, "order_type": r.order_type}
            for r in succeeded
        ],
        "failed": [
            {"account": r.account_name, "error": r.error}
            for r in failed
        ],
    }


@app.post("/close")
async def close(request: Request):
    """
    Receive a 'close_all' command and close all positions + orders for the symbol.

    Expected JSON body:
        {
          "action": "close_all",
          "symbol": "BTCUSDT"
        }
    """
    if not _authorized(request):
        logger.warning(f"Unauthorized request from {request.client.host}")
        return Response(content="Unauthorized", status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        data = await request.json()
    except Exception as exc:
        return {"status": "error", "error": f"Invalid JSON: {exc}"}

    symbol = data.get("symbol", "")
    if not symbol:
        return {"status": "error", "error": "symbol is required"}

    logger.info(f"📩 /close: symbol={symbol}")

    req = TradeRequest(action="close_all", symbol=symbol)

    try:
        results = await execute_trade(config, req)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"💥 Unhandled exception in /close:\n{tb}")
        return {"status": "error", "error": str(exc)}

    succeeded = [r for r in results if r.success]
    failed    = [r for r in results if not r.success]

    return {
        "status":  "ok" if succeeded else "error",
        "symbol":  symbol,
        "succeeded": [r.account_name for r in succeeded],
        "failed":    [{"account": r.account_name, "error": r.error} for r in failed],
    }
