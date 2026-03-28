"""
config.py
---------
Loads and validates all configuration from environment variables / .env file.

Usage:
    from config import Config
    cfg = Config()
    print(cfg.MAX_POSITION_SIZE)
"""

import os
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class Config:
    """
    Typed configuration dataclass populated from environment variables.
    Call load_dotenv() before instantiating, or pass a custom env_file path.
    """

    # ── Wallet / Authentication ───────────────────────────────────────────
    PRIVATE_KEY: str = ""
    POLYMARKET_PROXY_ADDRESS: str = ""
    SIGNATURE_TYPE: int = 0         # 0=EOA, 1=Magic/email, 2=browser wallet

    # ── Copy trading ─────────────────────────────────────────────────────
    TARGET_WALLET: str = ""

    # ── Trading mode ─────────────────────────────────────────────────────
    # Three modes: "paper" (log only), "micro" (real orders, tiny size),
    # "live" (full-size real orders).
    TRADING_MODE: str = "paper"     # Safe default — must explicitly change
    MICRO_TRADE_SIZE: float = 5.00  # USD per trade in micro mode
    # Legacy alias kept for backward compatibility with existing .env files
    PAPER_TRADE: bool = True        # Derived from TRADING_MODE; do not set directly

    # ── Risk / position limits ────────────────────────────────────────────
    MAX_POSITION_SIZE: float = 50.0
    MAX_TOTAL_EXPOSURE: float = 500.0
    MAX_POSITIONS: int = 10
    MIN_LIQUIDITY: float = 10_000.0
    KILL_SWITCH_THRESHOLD: float = -100.0

    # ── Strategy tuning ───────────────────────────────────────────────────
    ARBITRAGE_MIN_EDGE: float = 0.01
    COPY_TRADE_SIZE: float = 10.0
    COPY_TRADE_MAX_AGE: int = 120   # seconds
    SIGNAL_MIN_EDGE: float = 0.01
    MAX_SLIPPAGE: float = 0.03

    # ── Trade management (take-profit / stop-loss / exits) ─────────────
    TAKE_PROFIT_PCT: float = 0.15        # Close position at +15% unrealised P&L
    STOP_LOSS_PCT: float = 0.10          # Close position at -10% unrealised P&L
    MAX_HOLD_TIME: int = 86400           # Max seconds to hold (default: 24 h)
    TRAILING_STOP_ACTIVATION: float = 0.10  # Activate trailing stop after +10% gain
    TRAILING_STOP_PCT: float = 0.05      # Sell if price drops 5% from peak

    # ── Wallet auto-discovery ─────────────────────────────────────────────
    AUTO_DISCOVER_WALLETS: bool = True
    WALLET_DISCOVERY_INTERVAL: int = 21600   # Re-discover every 6 hours
    MIN_WIN_RATE: float = 0.55               # Minimum 55% win rate to qualify
    MIN_CLOSED_POSITIONS: int = 10           # Require at least 10 closed positions
    MAX_COPY_WALLETS: int = 3                # Follow top N discovered wallets
    WALLET_CATEGORIES: str = "OVERALL"      # Comma-separated leaderboard categories

    # ── Self-learning optimizer ───────────────────────────────────────────
    OPTIMIZER_ENABLED: bool = True
    OPTIMIZER_MIN_TRADES: int = 50       # Min trades before adapting
    OPTIMIZER_INTERVAL: int = 3600       # Re-optimize every N seconds (1 hour)
    OPTIMIZER_MAX_SHIFT: float = 0.15    # Max 15% parameter change per cycle
    OPTIMIZER_LOOKBACK: int = 200        # Analyse last N trades

    # ── LP Rewards Strategy ─────────────────────────────────────────────────
    LP_ENABLED: bool = True
    LP_CAPITAL_PCT: float = 0.20         # 20% of bankroll for LP
    LP_MAX_MARKETS: int = 5
    LP_REFRESH_INTERVAL: int = 300       # 5 minutes

    # ── Base Rate Rules ──────────────────────────────────────────────────
    BASE_RATE_MIN: float = 0.12          # 12% minimum base rate
    BASE_RATE_SIZE_CUT: float = 0.50     # Cut size by 50% if below threshold
    HOLD_TO_RESOLUTION: bool = True      # Prefer holding to resolution

    # ── Drawdown / consecutive-loss circuit breaker ────────────────────
    MAX_DAILY_DRAWDOWN_PCT: float = 0.05   # 5% of bankroll max daily loss
    MAX_CONSECUTIVE_LOSSES: int = 3        # Pause after N consecutive losses

    # ── Whale / large trade detection ────────────────────────────────
    WHALE_MIN_TRADE_USD: float = 5000.0    # Minimum USD for whale detection
    WHALE_LOOKBACK_MINUTES: int = 10       # Minutes to look back for spikes

    # ── Bayesian Re-evaluation ───────────────────────────────────────────
    REEVALUATE_INTERVAL: int = 10        # Every N cycles

    # ── Bot behaviour ─────────────────────────────────────────────────────
    POLL_INTERVAL: int = 30
    LOG_LEVEL: str = "INFO"

    def __post_init__(self) -> None:
        """Load values from environment after dataclass initialisation."""
        self._load_from_env()
        self._validate()

    def _load_from_env(self) -> None:
        """Populate fields from environment variables."""
        # Strings
        self.PRIVATE_KEY = os.getenv("PRIVATE_KEY", self.PRIVATE_KEY)
        self.POLYMARKET_PROXY_ADDRESS = os.getenv(
            "POLYMARKET_PROXY_ADDRESS", self.POLYMARKET_PROXY_ADDRESS
        )
        self.TARGET_WALLET = os.getenv("TARGET_WALLET", self.TARGET_WALLET)
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", self.LOG_LEVEL).upper()
        self.WALLET_CATEGORIES = os.getenv(
            "WALLET_CATEGORIES", self.WALLET_CATEGORIES
        )

        # Integers
        self.SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", str(self.SIGNATURE_TYPE)))
        self.MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", str(self.MAX_POSITIONS)))
        self.POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", str(self.POLL_INTERVAL)))
        self.COPY_TRADE_MAX_AGE = int(
            os.getenv("COPY_TRADE_MAX_AGE", str(self.COPY_TRADE_MAX_AGE))
        )

        # Floats
        self.MAX_POSITION_SIZE = float(
            os.getenv("MAX_POSITION_SIZE", str(self.MAX_POSITION_SIZE))
        )
        self.MAX_TOTAL_EXPOSURE = float(
            os.getenv("MAX_TOTAL_EXPOSURE", str(self.MAX_TOTAL_EXPOSURE))
        )
        self.MIN_LIQUIDITY = float(
            os.getenv("MIN_LIQUIDITY", str(self.MIN_LIQUIDITY))
        )
        self.KILL_SWITCH_THRESHOLD = float(
            os.getenv("KILL_SWITCH_THRESHOLD", str(self.KILL_SWITCH_THRESHOLD))
        )
        self.ARBITRAGE_MIN_EDGE = float(
            os.getenv("ARBITRAGE_MIN_EDGE", str(self.ARBITRAGE_MIN_EDGE))
        )
        self.COPY_TRADE_SIZE = float(
            os.getenv("COPY_TRADE_SIZE", str(self.COPY_TRADE_SIZE))
        )
        self.SIGNAL_MIN_EDGE = float(
            os.getenv("SIGNAL_MIN_EDGE", str(self.SIGNAL_MIN_EDGE))
        )
        self.MAX_SLIPPAGE = float(
            os.getenv("MAX_SLIPPAGE", str(self.MAX_SLIPPAGE))
        )

        # Floats — trade management
        self.TAKE_PROFIT_PCT = float(
            os.getenv("TAKE_PROFIT_PCT", str(self.TAKE_PROFIT_PCT))
        )
        self.STOP_LOSS_PCT = float(
            os.getenv("STOP_LOSS_PCT", str(self.STOP_LOSS_PCT))
        )
        self.TRAILING_STOP_ACTIVATION = float(
            os.getenv("TRAILING_STOP_ACTIVATION", str(self.TRAILING_STOP_ACTIVATION))
        )
        self.TRAILING_STOP_PCT = float(
            os.getenv("TRAILING_STOP_PCT", str(self.TRAILING_STOP_PCT))
        )
        self.MICRO_TRADE_SIZE = float(
            os.getenv("MICRO_TRADE_SIZE", str(self.MICRO_TRADE_SIZE))
        )
        self.MIN_WIN_RATE = float(
            os.getenv("MIN_WIN_RATE", str(self.MIN_WIN_RATE))
        )

        # Integers — trade management
        self.MAX_HOLD_TIME = int(
            os.getenv("MAX_HOLD_TIME", str(self.MAX_HOLD_TIME))
        )
        self.WALLET_DISCOVERY_INTERVAL = int(
            os.getenv("WALLET_DISCOVERY_INTERVAL", str(self.WALLET_DISCOVERY_INTERVAL))
        )
        self.MIN_CLOSED_POSITIONS = int(
            os.getenv("MIN_CLOSED_POSITIONS", str(self.MIN_CLOSED_POSITIONS))
        )
        self.MAX_COPY_WALLETS = int(
            os.getenv("MAX_COPY_WALLETS", str(self.MAX_COPY_WALLETS))
        )

        # ── LP Rewards Strategy ──────────────────────────────────────────
        lp_enabled_env = os.getenv("LP_ENABLED", "true").strip().lower()
        self.LP_ENABLED = lp_enabled_env in ("1", "true", "yes")
        self.LP_CAPITAL_PCT = float(os.getenv("LP_CAPITAL_PCT", str(self.LP_CAPITAL_PCT)))
        self.LP_MAX_MARKETS = int(os.getenv("LP_MAX_MARKETS", str(self.LP_MAX_MARKETS)))
        self.LP_REFRESH_INTERVAL = int(os.getenv("LP_REFRESH_INTERVAL", str(self.LP_REFRESH_INTERVAL)))

        # ── Base Rate Rules ──────────────────────────────────────────────
        self.BASE_RATE_MIN = float(os.getenv("BASE_RATE_MIN", str(self.BASE_RATE_MIN)))
        self.BASE_RATE_SIZE_CUT = float(os.getenv("BASE_RATE_SIZE_CUT", str(self.BASE_RATE_SIZE_CUT)))
        hold_env = os.getenv("HOLD_TO_RESOLUTION", "true").strip().lower()
        self.HOLD_TO_RESOLUTION = hold_env in ("1", "true", "yes")

        # ── Drawdown / consecutive-loss circuit breaker ──────────────────
        self.MAX_DAILY_DRAWDOWN_PCT = float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", str(self.MAX_DAILY_DRAWDOWN_PCT)))
        self.MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", str(self.MAX_CONSECUTIVE_LOSSES)))

        # ── Whale / large trade detection ─────────────────────────────
        self.WHALE_MIN_TRADE_USD = float(os.getenv("WHALE_MIN_TRADE_USD", str(self.WHALE_MIN_TRADE_USD)))
        self.WHALE_LOOKBACK_MINUTES = int(os.getenv("WHALE_LOOKBACK_MINUTES", str(self.WHALE_LOOKBACK_MINUTES)))

        # ── Bayesian Re-evaluation ───────────────────────────────────────
        self.REEVALUATE_INTERVAL = int(os.getenv("REEVALUATE_INTERVAL", str(self.REEVALUATE_INTERVAL)))

        # ── Trading mode ─────────────────────────────────────────────────
        # New TRADING_MODE env var takes precedence.
        # If not set, fall back to the legacy PAPER_TRADE boolean for
        # backward compatibility with existing .env files.
        trading_mode_env = os.getenv("TRADING_MODE", "").strip().lower()
        if trading_mode_env in ("paper", "micro", "live"):
            self.TRADING_MODE = trading_mode_env
        else:
            # Legacy: map PAPER_TRADE=true -> "paper", false -> "live"
            paper_env = os.getenv("PAPER_TRADE", "true").strip().lower()
            if paper_env in ("1", "true", "yes"):
                self.TRADING_MODE = "paper"
            else:
                self.TRADING_MODE = "live"

        # Keep PAPER_TRADE in sync for any code that still reads it
        self.PAPER_TRADE = (self.TRADING_MODE == "paper")

        # Booleans — wallet discovery
        auto_discover_env = os.getenv("AUTO_DISCOVER_WALLETS", "true").strip().lower()
        self.AUTO_DISCOVER_WALLETS = auto_discover_env in ("1", "true", "yes")

        # Booleans — optimizer
        optimizer_env = os.getenv("OPTIMIZER_ENABLED", "true").strip().lower()
        self.OPTIMIZER_ENABLED = optimizer_env in ("1", "true", "yes")

        # Integers — optimizer
        self.OPTIMIZER_MIN_TRADES = int(
            os.getenv("OPTIMIZER_MIN_TRADES", str(self.OPTIMIZER_MIN_TRADES))
        )
        self.OPTIMIZER_INTERVAL = int(
            os.getenv("OPTIMIZER_INTERVAL", str(self.OPTIMIZER_INTERVAL))
        )
        self.OPTIMIZER_LOOKBACK = int(
            os.getenv("OPTIMIZER_LOOKBACK", str(self.OPTIMIZER_LOOKBACK))
        )

        # Floats — optimizer
        self.OPTIMIZER_MAX_SHIFT = float(
            os.getenv("OPTIMIZER_MAX_SHIFT", str(self.OPTIMIZER_MAX_SHIFT))
        )

    def _validate(self) -> None:
        """Raise ValueError on obviously invalid configuration."""
        if not self.PRIVATE_KEY:
            raise ValueError(
                "PRIVATE_KEY is required. Set it in your .env file.\n"
                "Export your key from https://reveal.polymarket.com"
            )

        if self.SIGNATURE_TYPE not in (0, 1, 2):
            raise ValueError("SIGNATURE_TYPE must be 0 (EOA), 1 (Magic), or 2 (browser).")

        if self.SIGNATURE_TYPE in (1, 2) and not self.POLYMARKET_PROXY_ADDRESS:
            raise ValueError(
                "POLYMARKET_PROXY_ADDRESS is required for signature types 1 and 2."
            )

        if self.MAX_POSITION_SIZE <= 0:
            raise ValueError("MAX_POSITION_SIZE must be positive.")

        if self.MAX_TOTAL_EXPOSURE < self.MAX_POSITION_SIZE:
            raise ValueError(
                "MAX_TOTAL_EXPOSURE must be >= MAX_POSITION_SIZE."
            )

        if self.TRADING_MODE == "paper":
            logger.warning(
                "TRADING_MODE=paper — orders will be logged but NOT submitted."
            )
        elif self.TRADING_MODE == "micro":
            logger.warning(
                "TRADING_MODE=micro — real orders will be placed with MICRO_TRADE_SIZE=$%.2f per trade.",
                self.MICRO_TRADE_SIZE,
            )

    @property
    def funder_address(self) -> str:
        """
        Return the effective funder address.
        For EOA (sig type 0) with no proxy, the key's derived address is used
        automatically by the SDK.  For proxy-based accounts, use the proxy.
        """
        return self.POLYMARKET_PROXY_ADDRESS or ""

    def summary(self) -> str:
        """Return a human-readable config summary (redacts private key)."""
        pk_display = (
            self.PRIVATE_KEY[:6] + "..." + self.PRIVATE_KEY[-4:]
            if len(self.PRIVATE_KEY) > 10
            else "***"
        )
        return (
            f"Config("
            f"mode={self.TRADING_MODE}, "
            f"sig_type={self.SIGNATURE_TYPE}, "
            f"pk={pk_display}, "
            f"proxy={self.POLYMARKET_PROXY_ADDRESS or 'none'}, "
            f"max_pos=${self.MAX_POSITION_SIZE}, "
            f"max_exp=${self.MAX_TOTAL_EXPOSURE}, "
            f"max_n={self.MAX_POSITIONS}, "
            f"poll={self.POLL_INTERVAL}s, "
            f"tp={self.TAKE_PROFIT_PCT:.0%}, "
            f"sl={self.STOP_LOSS_PCT:.0%}, "
            f"max_hold={self.MAX_HOLD_TIME // 3600}h"
            f")"
        )


def load_config(env_file: str = ".env") -> Config:
    """
    Load .env file and return a validated Config object.

    Args:
        env_file: Path to the .env file (default: .env in CWD).

    Returns:
        Populated and validated Config instance.
    """
    load_dotenv(env_file, override=True)
    return Config()
