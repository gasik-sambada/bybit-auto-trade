"""Bybit API wrapper for bybit-auto-trade.

Uses raw httpx calls with HMAC-SHA256 request signing (Bybit v5 API).
Supports SPOT and FUTURES (linear perpetuals).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import ssl
import time
from typing import Any, Dict, Optional

import certifi
import httpx

from .config import AccountConfig, SymbolConfig

logger = logging.getLogger(__name__)

# Bybit v5 API timeout
_TIMEOUT = 15.0

# ─── SSL context ──────────────────────────────────────────────────────────────
# Force TLS 1.2+ and use certifi's up-to-date CA bundle.
# This fixes SSL handshake failures on servers with outdated system OpenSSL.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2

# Shared httpx client — reused across all requests for connection pooling.
# verify= accepts an ssl.SSLContext directly in httpx.
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    """Return the shared httpx client, creating it on first call."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=_TIMEOUT,
            verify=_SSL_CONTEXT,
        )
    return _HTTP_CLIENT


# ─── Request signing ──────────────────────────────────────────────────────────

def _sign(api_secret: str, timestamp: str, api_key: str, recv_window: str, body: str) -> str:
    """Generate HMAC-SHA256 signature for Bybit v5 POST requests."""
    param_str = timestamp + api_key + recv_window + body
    return hmac.new(
        api_secret.encode("utf-8"),
        param_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _headers(account: AccountConfig, body: str, recv_window: int = 5000) -> Dict[str, str]:
    """Build signed request headers for Bybit v5."""
    ts = str(int(time.time() * 1000))
    rw = str(recv_window)
    sig = _sign(account.api_secret, ts, account.api_key, rw, body)
    return {
        "X-BAPI-API-KEY":     account.api_key,
        "X-BAPI-TIMESTAMP":   ts,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-RECV-WINDOW": rw,
        "Content-Type":       "application/json",
    }


# ─── BybitClient ──────────────────────────────────────────────────────────────

class BybitClient:
    """Thin async Bybit v5 client for order placement and position management."""

    def __init__(self, account: AccountConfig) -> None:
        self._account = account
        self._base = account.base_url

    async def _post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Sign and POST to a Bybit v5 endpoint. Raises on HTTP or API errors."""
        import json as _json
        body = _json.dumps(payload, separators=(",", ":"))
        headers = _headers(self._account, body)
        url = f"{self._base}{endpoint}"

        logger.debug(f"[{self._account.name}] POST {url} body={body}")

        client = _get_http_client()
        resp = await client.post(url, content=body, headers=headers)

        data = resp.json()
        ret_code = data.get("retCode", -1)

        if resp.status_code != 200 or ret_code != 0:
            msg = data.get("retMsg", "unknown error")
            logger.error(
                f"[{self._account.name}] Bybit API error {ret_code}: {msg} | url={url}"
            )
            raise RuntimeError(f"Bybit API error {ret_code}: {msg}")

        logger.debug(f"[{self._account.name}] Response: {data}")
        return data

    async def _get(self, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Signed GET for Bybit v5."""
        import json as _json
        import urllib.parse as _up
        ts = str(int(time.time() * 1000))
        rw = "5000"
        query_str = _up.urlencode(params)
        param_str = ts + self._account.api_key + rw + query_str
        sig = hmac.new(
            self._account.api_secret.encode(),
            param_str.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY":     self._account.api_key,
            "X-BAPI-TIMESTAMP":   ts,
            "X-BAPI-SIGN":        sig,
            "X-BAPI-RECV-WINDOW": rw,
        }
        url = f"{self._base}{endpoint}"
        client = _get_http_client()
        resp = await client.get(url, params=params, headers=headers)
        return resp.json()

    # ── Leverage ──────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol_cfg: SymbolConfig, leverage: int) -> None:
        """Set leverage for a FUTURES symbol (both buy and sell side)."""
        lev_str = str(leverage)
        try:
            await self._post("/v5/position/set-leverage", {
                "category":     "linear",
                "symbol":       symbol_cfg.api_symbol,
                "buyLeverage":  lev_str,
                "sellLeverage": lev_str,
            })
            logger.info(f"[{self._account.name}] Leverage set to {leverage}x for {symbol_cfg.symbol}")
        except RuntimeError as exc:
            # Error code 110043 = leverage not modified (already set) — safe to ignore
            if "110043" in str(exc):
                logger.debug(f"[{self._account.name}] Leverage already {leverage}x for {symbol_cfg.symbol}")
            else:
                raise

    # ── Place order ───────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol_cfg: SymbolConfig,
        side: str,            # "Buy" or "Sell"
        qty: str,
        order_type: str,      # "Market" or "Limit"
        price: Optional[str], # Required for Limit; None for Market
        sl: Optional[str],
        tp: Optional[str],
    ) -> Dict[str, Any]:
        """Place a single order on Bybit."""
        category = "linear" if symbol_cfg.is_futures() else "spot"
        api_sym = symbol_cfg.api_symbol

        payload: Dict[str, Any] = {
            "category":    category,
            "symbol":      api_sym,
            "side":        side,           # "Buy" or "Sell"
            "orderType":   order_type,     # "Market" or "Limit"
            "qty":         qty,
            "timeInForce": "GTC" if order_type == "Limit" else "IOC",
        }

        if order_type == "Limit":
            if not price:
                raise ValueError("price is required for Limit orders")
            payload["price"] = price

        # SL/TP only for FUTURES
        if symbol_cfg.is_futures():
            if sl:
                payload["stopLoss"] = sl
            if tp:
                payload["takeProfit"] = tp
            # Hedge-mode: positionIdx=0 means one-way mode
            payload["positionIdx"] = 0

        logger.info(
            f"[{self._account.name}] PlaceOrder {order_type} {side} {qty} {symbol_cfg.symbol} "
            f"(api={api_sym}) price={price} sl={sl} tp={tp}"
        )
        return await self._post("/v5/order/create", payload)

    # ── Cancel & Close ────────────────────────────────────────────────────────

    async def cancel_all_orders(self, symbol_cfg: SymbolConfig) -> None:
        """Cancel all open orders for a symbol."""
        category = "linear" if symbol_cfg.is_futures() else "spot"
        try:
            await self._post("/v5/order/cancel-all", {
                "category": category,
                "symbol":   symbol_cfg.api_symbol,
            })
            logger.info(f"[{self._account.name}] Cancelled all orders for {symbol_cfg.symbol}")
        except RuntimeError as exc:
            logger.warning(f"[{self._account.name}] cancel-all failed for {symbol_cfg.symbol}: {exc}")

    async def get_positions(self, symbol_cfg: SymbolConfig) -> list:
        """Fetch open positions for a FUTURES symbol."""
        data = await self._get("/v5/position/list", {
            "category": "linear",
            "symbol":   symbol_cfg.api_symbol,
        })
        return (data.get("result") or {}).get("list") or []

    async def close_position(self, symbol_cfg: SymbolConfig, side: str, size: str) -> None:
        """Close a specific position by placing a reduce-only market order."""
        close_side = "Sell" if side == "Buy" else "Buy"
        await self._post("/v5/order/create", {
            "category":    "linear",
            "symbol":      symbol_cfg.api_symbol,
            "side":        close_side,
            "orderType":   "Market",
            "qty":         size,
            "reduceOnly":  True,
            "timeInForce": "IOC",
            "positionIdx": 0,
        })
        logger.info(
            f"[{self._account.name}] Closed position {symbol_cfg.symbol} side={side} size={size}"
        )

    async def cancel_and_close_all(self, symbol_cfg: SymbolConfig, close_side: str = "") -> None:
        """Cancel all orders, then close open positions for a symbol.

        Args:
            symbol_cfg:  Symbol configuration.
            close_side:  Optional Bybit side to filter ("Buy" or "Sell").
                         When set, only positions on that side are closed.
                         When empty, ALL positions for the symbol are closed.
        """
        await self.cancel_all_orders(symbol_cfg)

        if symbol_cfg.is_futures():
            positions = await self.get_positions(symbol_cfg)
            for pos in positions:
                size = pos.get("size", "0")
                side = pos.get("side", "")
                if not size or size in ("0", "0.0", "") or not side:
                    continue
                # If a specific side was requested, skip positions on the other side
                if close_side and side != close_side:
                    logger.info(
                        f"[{self._account.name}] Skipping {side} position for "
                        f"{symbol_cfg.symbol} (close_side={close_side})"
                    )
                    continue
                await self.close_position(symbol_cfg, side, size)

    async def update_position_sl_tp(
        self,
        symbol_cfg: SymbolConfig,
        sl: Optional[str] = None,
        tp: Optional[str] = None,
    ) -> None:
        """
        Update stop-loss and/or take-profit on all open positions for a FUTURES symbol.

        Uses Bybit v5 /v5/position/trading-stop.
        sl / tp can be "0" to remove the existing SL/TP.
        """
        if not symbol_cfg.is_futures():
            raise ValueError(f"update_position_sl_tp is only supported for FUTURES symbols, got {symbol_cfg.symbol}")

        positions = await self.get_positions(symbol_cfg)
        if not positions:
            logger.warning(f"[{self._account.name}] No open positions for {symbol_cfg.symbol} — skipping SL/TP update")
            return

        for pos in positions:
            size = pos.get("size", "0")
            side = pos.get("side", "")
            if not size or size in ("0", "0.0", "") or not side:
                continue  # Skip closed/empty positions

            payload: Dict[str, Any] = {
                "category":    "linear",
                "symbol":      symbol_cfg.api_symbol,
                "positionIdx": 0,  # one-way mode
            }
            if sl is not None:
                payload["stopLoss"] = sl
            if tp is not None:
                payload["takeProfit"] = tp

            await self._post("/v5/position/trading-stop", payload)
            logger.info(
                f"[{self._account.name}] Updated SL/TP for {symbol_cfg.symbol} "
                f"side={side} sl={sl} tp={tp}"
            )
