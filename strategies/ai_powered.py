"""
strategies/ai_powered.py
-------------------------
AI-powered trading strategy that uses Claude to estimate true probabilities
for Polymarket markets, then trades when the market price diverges
significantly from the AI's estimate.

This strategy:
  1. Scans all non-crypto markets.
  2. Prioritizes markets with mid-range prices (most likely to be mispriced).
  3. Gathers real-world news/data relevant to each market.
  4. Sends the market question + context to Claude for probability estimation.
  5. Compares Claude's estimate to the market price.
  6. Generates BUY signals when the edge exceeds AI_MIN_EDGE (default 8%).

The strategy is rate-limited to MAX_AI_EVALUATIONS per cycle to control
API costs (~$0.003 per evaluation).

Configuration (.env):
  ANTHROPIC_API_KEY: Your Anthropic API key (required)
  AI_MODEL: Claude model (default: claude-sonnet-4-20250514)
  AI_MIN_EDGE: Minimum edge to trade (default: 0.08 = 8%)
"""

import logging
from typing import List, Optional

from strategies.base import BaseStrategy, TradeSignal
from ai_probability_engine import AIProbabilityEngine, ProbabilityEstimate
from market_scanner import MarketInfo

logger = logging.getLogger(__name__)


class AIPoweredStrategy(BaseStrategy):
    """
    Uses Claude AI to identify mispriced prediction markets.

    Evaluates up to 10 markets per cycle, generates signals when
    the AI's probability estimate diverges from the market price
    by more than AI_MIN_EDGE.
    """

    def name(self) -> str:
        return "ai_powered"

    def __init__(self, *args, ai_engine=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._engine = ai_engine or AIProbabilityEngine(self.cfg)

    def scan(self) -> List[TradeSignal]:
        """
        Evaluate markets using Claude and generate signals where edge exists.
        """
        if not self._engine.enabled:
            self.log.debug("AI strategy disabled: no ANTHROPIC_API_KEY configured.")
            return []

        markets = self.market_scanner.get_markets()
        estimates = self._engine.evaluate_markets(markets)

        signals = []
        for est in estimates:
            signal = self._build_signal(est, markets)
            if signal:
                signals.append(signal)
                self._log_signal(signal)

        if signals:
            self.log.info("AI strategy produced %d signal(s).", len(signals))

        return signals

    def _build_signal(
        self, est: ProbabilityEstimate, markets: List[MarketInfo]
    ) -> Optional[TradeSignal]:
        """
        Convert a ProbabilityEstimate into a TradeSignal.
        """
        if est.recommended_side == "SKIP":
            return None

        # Find the market info
        market = None
        for m in markets:
            if m.market_id == est.market_id:
                market = m
                break
        if not market:
            return None

        # Determine which token to buy
        if est.recommended_side == "BUY_YES":
            # AI thinks yes probability is higher than market price
            token = market.yes_token
            if not token:
                return None
            price = token.best_ask
            confidence_score = self._confidence_to_score(est)
        elif est.recommended_side == "BUY_NO":
            # AI thinks yes probability is lower than market price
            # → buy the No token
            token = market.no_token
            if not token:
                return None
            price = token.best_ask
            # For BUY_NO, the edge is inverted
            confidence_score = self._confidence_to_score(est)
        else:
            return None

        if price <= 0 or price >= 0.99:
            return None

        # Size based on confidence and edge
        edge_pct = abs(est.edge)
        budget = self.cfg.MAX_POSITION_SIZE * confidence_score * 0.5
        size = budget / price if price > 0 else 0
        size = max(round(size, 2), 5.0)

        reason = (
            f"AI [{est.category}] {est.recommended_side} | "
            f"est={est.estimated_probability:.1%} vs mkt={est.market_price:.1%} "
            f"edge={est.edge:+.1%} [{est.confidence}] | "
            f"{est.reasoning[:80]}"
        )

        return TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=token.token_id,
            side="BUY",
            price=round(price, 4),
            size=size,
            confidence=confidence_score,
            reason=reason,
            order_type="GTC",
        )

    @staticmethod
    def _confidence_to_score(est: ProbabilityEstimate) -> float:
        """
        Convert Claude's confidence label + edge magnitude into a
        numeric score for the Kelly sizing and EV filter.
        """
        # Base from confidence label
        base = {"high": 0.80, "medium": 0.60, "low": 0.40}.get(
            est.confidence.lower(), 0.50
        )

        # Boost from edge magnitude
        edge_boost = min(abs(est.edge) * 2, 0.15)

        return min(base + edge_boost, 0.95)
