"""Trade execution logic for bybit-auto-trade.

For each incoming trade command:
1. Find all accounts that have the symbol enabled.
2. Resolve symbol config (market_type, leverage, order_type).
3. Calculate order quantity using risk-based sizing:
      qty = min_loss_usd / |entry_price - sl_price| / leverage   (FUTURES)
      qty = min_loss_usd / |entry_price - sl_price|              (SPOT, leverage=1)
4. Set leverage on Bybit (FUTURES only).
5. Place the order.

The order_type can be overridden per-alert from TradingView.
If not provided in the alert, the per-symbol default from symbols.yaml is used.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import List, Optional

from .bybit_client import BybitClient
from .config import AccountConfig, Config, SymbolConfig

logger = logging.getLogger(__name__)


@dataclass
class TradeRequest:
    """Parsed incoming trade command."""

    action: str           # "open", "close_all", or "update_sl_tp"
    symbol: str           # e.g. "BTCUSDT.P" (with .P suffix for perpetuals)
    side: str = ""        # "BUY" or "SELL" (for "open")
    price: str = ""       # entry price as string (for Limit orders)
    sl: str = ""          # stop-loss price as string
    tp: str = ""          # take-profit price as string
    order_type: str = "" # "Market" or "Limit" (optional override from alert)


@dataclass
class TradeResult:
    """Result of a single account trade attempt."""

    account_name: str
    symbol: str
    success: bool
    qty: Optional[str] = None
    order_type: Optional[str] = None
    error: Optional[str] = None


def _calculate_qty(
    min_loss_usd: float,
    entry_price: float,
    sl_price: float,
    leverage: int,
    is_futures: bool,
) -> float:
    """
    Risk-based qty calculation.

    For FUTURES: qty = min_loss_usd / (|entry - sl| * leverage)
        The dollar value of 1 unit move = 1 USD for linear perpetuals.
        With leverage the effective risk per unit = |entry - sl| (leverage already
        in the position, not in the qty formula — qty is in base-coin units).

    For SPOT (leverage=1): qty = min_loss_usd / |entry - sl|
    """
    sl_distance = abs(entry_price - sl_price)
    if sl_distance == 0:
        raise ValueError("stop-loss price equals entry price — cannot calculate qty")

    if is_futures:
        # Linear perpetual: PnL = qty * |price_change|
        # Risk = qty * sl_distance = min_loss_usd
        qty = min_loss_usd / sl_distance
    else:
        qty = min_loss_usd / sl_distance

    return qty


def _format_qty(qty: float, symbol_cfg: SymbolConfig) -> str:
    """Format qty to a reasonable precision string for Bybit."""
    # Bybit usually requires up to 3 decimal places for most symbols
    # Use up to 4 significant decimals, strip trailing zeros
    formatted = f"{qty:.4f}".rstrip("0").rstrip(".")
    return formatted if formatted else "0"


async def execute_trade(cfg: Config, req: TradeRequest) -> List[TradeResult]:
    """
    Execute a trade command across all accounts that have the symbol enabled.
    Returns a list of results, one per account.
    """
    results: List[TradeResult] = []

    symbol = req.symbol.upper()

    # Look up symbol config
    symbol_cfg = cfg.get_symbol(symbol)
    if symbol_cfg is None:
        logger.warning(f"[Trader] Symbol '{symbol}' not in symbols.yaml — ignoring")
        return [TradeResult(
            account_name="(none)",
            symbol=symbol,
            success=False,
            error=f"Symbol '{symbol}' not configured in symbols.yaml",
        )]

    accounts = cfg.get_accounts_for_symbol(symbol)
    if not accounts:
        logger.warning(f"[Trader] No accounts configured for symbol '{symbol}'")
        return [TradeResult(
            account_name="(none)",
            symbol=symbol,
            success=False,
            error=f"No accounts have symbol '{symbol}' enabled",
        )]

    for account in accounts:
        result = await _execute_for_account(account, symbol_cfg, req, cfg)
        results.append(result)

    return results


async def _execute_for_account(
    account: AccountConfig,
    symbol_cfg: SymbolConfig,
    req: TradeRequest,
    cfg: Config,
) -> TradeResult:
    """Execute a single trade for one account."""
    client = BybitClient(account)

    if req.action == "close_all":
        return await _handle_close_all(client, account, symbol_cfg)

    if req.action == "update_sl_tp":
        return await _handle_update_sl_tp(client, account, symbol_cfg, req)

    if req.action == "open":
        return await _handle_open(client, account, symbol_cfg, req)

    return TradeResult(
        account_name=account.name,
        symbol=symbol_cfg.symbol,
        success=False,
        error=f"Unknown action: {req.action!r}",
    )


async def _handle_close_all(
    client: BybitClient,
    account: AccountConfig,
    symbol_cfg: SymbolConfig,
) -> TradeResult:
    """Handle close_all action: cancel orders + close positions."""
    try:
        await client.cancel_and_close_all(symbol_cfg)
        return TradeResult(
            account_name=account.name,
            symbol=symbol_cfg.symbol,
            success=True,
        )
    except Exception as exc:
        logger.error(f"[{account.name}] close_all failed for {symbol_cfg.symbol}: {exc}")
        return TradeResult(
            account_name=account.name,
            symbol=symbol_cfg.symbol,
            success=False,
            error=str(exc),
        )


async def _handle_update_sl_tp(
    client: BybitClient,
    account: AccountConfig,
    symbol_cfg: SymbolConfig,
    req: TradeRequest,
) -> TradeResult:
    """Handle update_sl_tp action: update SL/TP on open FUTURES positions."""
    if not symbol_cfg.is_futures():
        return TradeResult(
            account_name=account.name,
            symbol=symbol_cfg.symbol,
            success=False,
            error="update_sl_tp is only supported for FUTURES (symbol with .P suffix)",
        )

    sl = req.sl or None
    tp = req.tp or None

    if sl is None and tp is None:
        return TradeResult(
            account_name=account.name,
            symbol=symbol_cfg.symbol,
            success=False,
            error="update_sl_tp requires at least sl or tp value",
        )

    try:
        await client.update_position_sl_tp(symbol_cfg, sl=sl, tp=tp)
        return TradeResult(
            account_name=account.name,
            symbol=symbol_cfg.symbol,
            success=True,
        )
    except Exception as exc:
        logger.error(f"[{account.name}] update_sl_tp failed for {symbol_cfg.symbol}: {exc}")
        return TradeResult(
            account_name=account.name,
            symbol=symbol_cfg.symbol,
            success=False,
            error=str(exc),
        )


async def _handle_open(
    client: BybitClient,
    account: AccountConfig,
    symbol_cfg: SymbolConfig,
    req: TradeRequest,
) -> TradeResult:
    """Handle open action: calculate qty, set leverage, place order."""
    symbol = symbol_cfg.symbol

    # ── Resolve order type ────────────────────────────────────────────────────
    # Priority: alert payload > symbol yaml default
    effective_order_type = req.order_type or symbol_cfg.order_type

    # ── Parse prices ─────────────────────────────────────────────────────────
    try:
        entry_price = float(req.price) if req.price else 0.0
        sl_price = float(req.sl) if req.sl else 0.0
        tp_price = float(req.tp) if req.tp else None
    except ValueError as exc:
        return TradeResult(
            account_name=account.name, symbol=symbol, success=False,
            error=f"Invalid price value: {exc}",
        )

    if sl_price == 0:
        return TradeResult(
            account_name=account.name, symbol=symbol, success=False,
            error="sl (stop-loss) price is required and must be > 0",
        )
    if effective_order_type == "Limit" and entry_price == 0:
        return TradeResult(
            account_name=account.name, symbol=symbol, success=False,
            error="entry price is required for Limit orders",
        )

    # Use sl_price to compute SL distance; for Market orders, use sl_price vs sl itself
    # For qty calc when Market order, we still need entry price for risk calc
    ref_price = entry_price if entry_price > 0 else sl_price

    # ── Resolve leverage ──────────────────────────────────────────────────────
    leverage = account.get_leverage(symbol, symbol_cfg)

    # ── Calculate qty ─────────────────────────────────────────────────────────
    try:
        qty_float = _calculate_qty(
            min_loss_usd=account.min_loss_usd,
            entry_price=ref_price,
            sl_price=sl_price,
            leverage=leverage,
            is_futures=symbol_cfg.is_futures(),
        )
    except ValueError as exc:
        return TradeResult(
            account_name=account.name, symbol=symbol, success=False,
            error=str(exc),
        )

    qty_str = _format_qty(qty_float, symbol_cfg)

    logger.info(
        f"[{account.name}] {symbol} qty={qty_str} "
        f"(min_loss=${account.min_loss_usd}, "
        f"sl_dist={abs(ref_price - sl_price):.4f}, lev={leverage}x)"
    )

    # ── Set leverage (FUTURES only) ───────────────────────────────────────────
    if symbol_cfg.is_futures():
        try:
            await client.set_leverage(symbol_cfg, leverage)
        except Exception as exc:
            logger.error(f"[{account.name}] Failed to set leverage for {symbol}: {exc}")
            return TradeResult(
                account_name=account.name, symbol=symbol, success=False,
                error=f"set_leverage failed: {exc}",
            )

    # ── Convert side to Bybit format ──────────────────────────────────────────
    # TradingView: BUY/SELL → Bybit: Buy/Sell
    bybit_side = "Buy" if req.side.upper() in ("BUY", "LONG") else "Sell"

    # ── Place order ───────────────────────────────────────────────────────────
    try:
        await client.place_order(
            symbol_cfg=symbol_cfg,
            side=bybit_side,
            qty=qty_str,
            order_type=effective_order_type,
            price=req.price if effective_order_type == "Limit" else None,
            sl=req.sl or None,
            tp=req.tp or None,
        )
        return TradeResult(
            account_name=account.name,
            symbol=symbol,
            success=True,
            qty=qty_str,
            order_type=effective_order_type,
        )
    except Exception as exc:
        logger.error(f"[{account.name}] place_order failed for {symbol}: {exc}")
        return TradeResult(
            account_name=account.name, symbol=symbol, success=False,
            error=str(exc),
        )
