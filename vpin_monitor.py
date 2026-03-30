"""
vpin_monitor.py
---------------
Volume-Synchronized Probability of Informed Trading (VPIN) monitor.

VPIN measures the imbalance between buy and sell volume in a market.
When VPIN spikes, it signals "toxic flow" — meaning informed traders
(insiders, bots with faster data) are flooding one side of the book.

Formula:
    VPIN = |V_buy - V_sell| / (V_buy + V_sell)

    VPIN = 0.0 → perfectly balanced (safe)
    VPIN = 0.5 → moderate imbalance
    VPIN = 1.0 → completely one-sided (toxic)

When VPIN exceeds the threshold, the bot should avoid entering that market
because someone likely knows something we don't.

Usage:
    monitor = VPINMonitor()
    monitor.record_trade(market_id, side="BUY", usd_value=100)
    if monitor.is_toxic(market_id):
        skip_this_market()
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

logger = logging.getLogger("bot.vpin")

# VPIN above this threshold = toxic flow, avoid market
VPIN_TOXIC_THRESHOLD = 0.70

# Rolling window for VPIN calculation (seconds)
VPIN_WINDOW = 300  # 5 minutes

# Minimum trades in window to calculate VPIN (avoid noise on thin data)
VPIN_MIN_TRADES = 5


@dataclass
class MarketFlow:
    """Tracks buy/sell volume for a single market."""
    # (timestamp, usd_value) tuples
    buys: List[Tuple[float, float]] = field(default_factory=list)
    sells: List[Tuple[float, float]] = field(default_factory=list)


class VPINMonitor:
    """
    Monitors order flow toxicity per market using VPIN.

    Strategies should check is_toxic(market_id) before entering a position.
    """

    def __init__(
        self,
        threshold: float = VPIN_TOXIC_THRESHOLD,
        window: int = VPIN_WINDOW,
    ) -> None:
        self.threshold = threshold
        self.window = window
        self._flows: Dict[str, MarketFlow] = defaultdict(MarketFlow)

    def record_trade(self, market_id: str, side: str, usd_value: float) -> None:
        """
        Record an observed trade for VPIN calculation.

        Call this with trade data from the Data API, WebSocket, or
        whale detector when trades are observed in a market.
        """
        now = time.time()
        flow = self._flows[market_id]

        if side.upper() in ("BUY", "LONG"):
            flow.buys.append((now, usd_value))
        elif side.upper() in ("SELL", "SHORT"):
            flow.sells.append((now, usd_value))

        # Prune old entries
        self._prune(flow)

    def get_vpin(self, market_id: str) -> float:
        """
        Calculate current VPIN for a market.

        Returns:
            VPIN value between 0.0 (balanced) and 1.0 (completely one-sided).
            Returns 0.0 if insufficient data.
        """
        flow = self._flows.get(market_id)
        if not flow:
            return 0.0

        self._prune(flow)

        n_trades = len(flow.buys) + len(flow.sells)
        if n_trades < VPIN_MIN_TRADES:
            return 0.0

        v_buy = sum(v for _, v in flow.buys)
        v_sell = sum(v for _, v in flow.sells)
        total = v_buy + v_sell

        if total <= 0:
            return 0.0

        return abs(v_buy - v_sell) / total

    def is_toxic(self, market_id: str) -> bool:
        """Check if a market has toxic order flow (VPIN above threshold)."""
        vpin = self.get_vpin(market_id)
        if vpin >= self.threshold:
            logger.info(
                "VPIN toxic: market %s VPIN=%.2f >= %.2f (avoiding)",
                market_id[:16], vpin, self.threshold,
            )
            return True
        return False

    def get_all_vpin(self) -> Dict[str, float]:
        """Return VPIN values for all tracked markets."""
        return {
            mid: self.get_vpin(mid)
            for mid in self._flows
        }

    def _prune(self, flow: MarketFlow) -> None:
        """Remove entries outside the rolling window."""
        cutoff = time.time() - self.window
        flow.buys = [(t, v) for t, v in flow.buys if t > cutoff]
        flow.sells = [(t, v) for t, v in flow.sells if t > cutoff]
