"""Configuration loader for bybit-auto-trade.

Accounts are discovered by scanning for ACCOUNT_N_NAME environment variables
(N = 1, 2, 3, …). Each account may override the global symbol leverage via
ACCOUNT_N_<SYMBOL>_LEVERAGE.

Symbol naming convention:
  - BTCUSDT.P  → Bybit linear perpetual (FUTURES, category=linear)
  - BTCUSDT    → Bybit spot (SPOT, category=spot)

  The .P suffix mirrors TradingView's naming for perpetual contracts.
  When making Bybit API calls the .P suffix is stripped (api_symbol property).

Symbol-level settings (market_type, default order_type) come from
config/symbols.yaml which is mounted into the container at /app/config/.
"""
from __future__ import annotations

import logging
import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ─── Symbol config (from YAML) ────────────────────────────────────────────────

@dataclass
class SymbolConfig:
    """Per-symbol trading parameters.

    symbol:      Full symbol key including .P suffix for perpetuals, e.g. BTCUSDT.P
    api_symbol:  Bybit API symbol name (no suffix), e.g. BTCUSDT
    market_type: FUTURES (linear perpetual) or SPOT
    qty_step:    Bybit lot size step. Round qty DOWN to this value before placing.
                 Check Bybit's instrument info for each symbol.
                 Examples: BTC=0.001, ETH=0.01, SOL=0.1, MNT=1, XRP=1
    """

    symbol: str
    market_type: str = "FUTURES"   # "FUTURES" or "SPOT"
    leverage: int = 10             # default leverage (overridable per account)
    order_type: str = "Market"     # "Market" or "Limit"
    qty_step: float = 0.001        # lot size step (round down qty to this)

    @property
    def api_symbol(self) -> str:
        """Return the Bybit API symbol name — strips .P suffix for perpetuals."""
        sym = self.symbol.upper()
        if sym.endswith(".P"):
            return sym[:-2]
        return sym

    def is_futures(self) -> bool:
        return self.market_type.upper() == "FUTURES"


# ─── Account config (from environment) ────────────────────────────────────────

@dataclass
class AccountConfig:
    """Per-account Bybit credentials and trading preferences."""

    name: str
    api_key: str
    api_secret: str
    prod_env: bool = False
    # Symbols this account trades (uppercase, with .P suffix for perpetuals)
    symbols: Set[str] = field(default_factory=set)
    # Minimum loss per trade in USD (used for qty calculation)
    min_loss_usd: float = 10.0
    # Per-symbol leverage overrides: symbol → leverage
    leverage_overrides: Dict[str, int] = field(default_factory=dict)

    def get_leverage(self, symbol: str, symbol_cfg: SymbolConfig) -> int:
        """Return the effective leverage for a symbol (override > yaml default)."""
        return self.leverage_overrides.get(symbol.upper(), symbol_cfg.leverage)

    @property
    def base_url(self) -> str:
        return "https://api.bybit.com" if self.prod_env else "https://api-testnet.bybit.com"


# ─── Top-level Config ──────────────────────────────────────────────────────────

@dataclass
class Config:
    """Full application configuration."""

    accounts: List[AccountConfig] = field(default_factory=list)
    symbols: Dict[str, SymbolConfig] = field(default_factory=dict)

    # Service security
    auto_trade_secret: str = ""

    # Server settings
    host: str = "0.0.0.0"
    port: int = 9000
    log_level: str = "INFO"

    @property
    def enabled_symbols(self) -> Set[str]:
        return set(self.symbols.keys())

    def get_symbol(self, symbol: str) -> Optional[SymbolConfig]:
        """Look up symbol config (case-insensitive)."""
        return self.symbols.get(symbol.upper())

    def get_accounts_for_symbol(self, symbol: str) -> List[AccountConfig]:
        """Return all accounts that have this symbol enabled."""
        upper = symbol.upper()
        return [acc for acc in self.accounts if upper in acc.symbols]

    @classmethod
    def load(cls, symbols_yaml_path: str = "config/symbols.yaml") -> "Config":
        """Load config from YAML file + environment variables."""
        symbols = _load_symbols_yaml(symbols_yaml_path)
        accounts = _load_accounts_from_env(symbols)

        return cls(
            accounts=accounts,
            symbols=symbols,
            auto_trade_secret=os.getenv("AUTO_TRADE_SECRET", ""),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "9000")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


# ─── Loaders ──────────────────────────────────────────────────────────────────

def _load_symbols_yaml(path: str) -> Dict[str, SymbolConfig]:
    """Parse config/symbols.yaml into a dict of SymbolConfig keyed by symbol."""
    yaml_path = Path(path)
    if not yaml_path.exists():
        logger.warning(f"symbols.yaml not found at {path}, using empty symbol list")
        return {}

    with yaml_path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    symbols: Dict[str, SymbolConfig] = {}
    for sym, cfg in (raw.get("symbols") or {}).items():
        sym_upper = sym.upper()
        # Auto-detect market_type from .P suffix if not explicitly set in YAML
        has_p_suffix = sym_upper.endswith(".P")
        default_market = "FUTURES" if has_p_suffix else "SPOT"
        market_type = str(cfg.get("market_type", default_market)).upper()
        symbols[sym_upper] = SymbolConfig(
            symbol=sym_upper,
            market_type=market_type,
            leverage=int(cfg.get("leverage", 10)),
            order_type=str(cfg.get("order_type", "Market")),
            qty_step=float(cfg.get("qty_step", 0.001)),
        )

    logger.info(f"Loaded {len(symbols)} symbols from {path}: {list(symbols.keys())}")
    return symbols


def _load_accounts_from_env(symbols: Dict[str, SymbolConfig]) -> List[AccountConfig]:
    """
    Discover accounts by scanning for ACCOUNT_N_NAME vars (N = 1, 2, 3, …).
    Stops as soon as a gap is found (e.g. ACCOUNT_1 + ACCOUNT_2 but no ACCOUNT_3).

    Per-account env vars:
        ACCOUNT_N_NAME             human-readable name (required to register account)
        ACCOUNT_N_API_KEY          Bybit API key
        ACCOUNT_N_API_SECRET       Bybit API secret
        ACCOUNT_N_PROD_ENV         true/false (default false)
        ACCOUNT_N_SYMBOLS          comma-separated symbols WITH .P for perpetuals
                                   e.g. "BTCUSDT.P,ETHUSDT.P,SOLUSDT"
        ACCOUNT_N_MIN_LOSS_USD     float, minimum loss in USD per trade (default 10)
        ACCOUNT_N_<SYMBOL>_LEVERAGE  int, per-symbol leverage override
                                     For .P symbols, use the full name:
                                     ACCOUNT_1_BTCUSDT.P_LEVERAGE=20
    """
    accounts: List[AccountConfig] = []
    n = 1
    while True:
        prefix = f"ACCOUNT_{n}_"
        name = os.getenv(f"{prefix}NAME", "")
        if not name:
            break  # No more accounts

        api_key = os.getenv(f"{prefix}API_KEY", "")
        api_secret = os.getenv(f"{prefix}API_SECRET", "")
        if not api_key or not api_secret:
            logger.warning(f"Account {n} ({name!r}) missing API_KEY or API_SECRET — skipping")
            n += 1
            continue

        prod_env = os.getenv(f"{prefix}PROD_ENV", "false").lower() in ("true", "1", "yes")

        raw_symbols = os.getenv(f"{prefix}SYMBOLS", "")
        account_symbols: Set[str] = set()
        for s in raw_symbols.split(","):
            s = s.strip().upper()
            if s:
                if s not in symbols:
                    logger.warning(
                        f"Account {n} ({name!r}): symbol '{s}' not in symbols.yaml — will be skipped"
                    )
                account_symbols.add(s)

        min_loss_usd = float(os.getenv(f"{prefix}MIN_LOSS_USD", "10"))

        # Per-symbol leverage overrides: ACCOUNT_N_BTCUSDT_LEVERAGE=20
        leverage_overrides: Dict[str, int] = {}
        for sym in account_symbols:
            lev_key = f"{prefix}{sym}_LEVERAGE"
            lev_val = os.getenv(lev_key)
            if lev_val:
                try:
                    leverage_overrides[sym] = int(lev_val)
                except ValueError:
                    logger.warning(f"Invalid leverage override for {lev_key}: {lev_val!r}")

        acc = AccountConfig(
            name=name,
            api_key=api_key,
            api_secret=api_secret,
            prod_env=prod_env,
            symbols=account_symbols,
            min_loss_usd=min_loss_usd,
            leverage_overrides=leverage_overrides,
        )
        accounts.append(acc)
        logger.info(
            f"Loaded account [{n}] {name!r}: prod={prod_env}, "
            f"symbols={sorted(account_symbols)}, min_loss=${min_loss_usd}"
        )
        n += 1

    if not accounts:
        logger.warning("No accounts configured! Set ACCOUNT_1_NAME, ACCOUNT_1_API_KEY, etc.")

    return accounts
