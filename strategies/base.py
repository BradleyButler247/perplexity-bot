"""
strategies/base.py
------------------
Abstract base class and shared data types for all trading strategies.

Every concrete strategy must:
  1. Inherit from BaseStrategy.
  2. Implement scan() to return a list of TradeSignal objects.
  3. Implement name() to return a unique strategy identifier string.
"""

import dataclasses
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from config import Config
    from market_scanner import MarketScanner
    from risk_manager import RiskManager
    from execution import Executor

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TradeSignal:
    """
    A trade opportunity identified by a strategy.

    All monetary values are in USDC on the Polygon network.
    Prices are in the range [0.00, 1.00].
    """

    strategy: str       # Name of the strategy that generated this signal
    market_id: str      # Polymarket condition ID (0x…)
    token_id: str       # Yes or No outcome token ID
    side: str           # "BUY" or "SELL"
    price: float        # Target limit price (0.01 – 0.99)
    size: float         # Number of shares to trade
    confidence: float   # 0.0 – 1.0 signal confidence score
    reason: str         # Human-readable explanation of the signal
    order_type: str     # "GTC" (limit) or "FOK" (market/immediate)

    def __str__(self) -> str:
        return (
            f"TradeSignal({self.strategy} | {self.side} {self.size:.2f} shares "
            f"@ ${self.price:.3f} | {self.token_id[:16]}… | {self.reason[:80]})"
        )

    @property
    def usd_value(self) -> float:
        """Approximate USD cost of this trade (price × size)."""
        return self.price * self.size


class BaseStrategy(ABC):
    """
    Abstract strategy interface.

    Subclasses receive shared references to core bot components (config,
    client, scanner, risk manager, executor) at construction time.  They must
    implement scan() to produce TradeSignal objects.
    """

    def __init__(
        self,
        cfg: "Config",
        client,
        market_scanner: "MarketScanner",
        risk_manager: "RiskManager",
        executor: "Executor",
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.market_scanner = market_scanner
        self.risk_manager = risk_manager
        self.executor = executor
        self.log = logging.getLogger(f"bot.strategy.{self.name()}")

    @abstractmethod
    def scan(self) -> List[TradeSignal]:
        """
        Scan for trading opportunities.

        Returns:
            List of TradeSignal objects.  Returns an empty list when no
            actionable opportunities are found.
        """
        raise NotImplementedError

    @abstractmethod
    def name(self) -> str:
        """Return a unique, human-readable strategy name."""
        raise NotImplementedError

    def _log_signal(self, signal: TradeSignal) -> None:
        """Emit a standardised INFO log entry for a discovered signal."""
        self.log.info(
            "Signal | market=%s | side=%s | size=%.2f | price=%.3f | "
            "confidence=%.2f | reason=%s",
            signal.market_id[:16],
            signal.side,
            signal.size,
            signal.price,
            signal.confidence,
            signal.reason[:120],
        )
