"""
ai_probability_engine.py
-------------------------
Uses Claude (Anthropic API) to estimate true probabilities for Polymarket
markets by analyzing the market question alongside real-world data.

Flow:
  1. NewsAggregator gathers relevant headlines, scores, prices, etc.
  2. This engine builds a structured prompt with the market question + context.
  3. Claude returns a probability estimate with confidence and reasoning.
  4. The estimate is compared to the market price to identify edge.

Configuration:
  ANTHROPIC_API_KEY: Set in .env (required for this module to function)
  AI_MODEL: Claude model to use (default: claude-sonnet-4-20250514)
  AI_MIN_EDGE: Minimum edge (|estimated_prob - market_price|) to signal

Cost: ~$0.003 per market evaluation (~$3/month at 50 markets/cycle, 1 cycle/min)
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from config import Config
from http_client import get_session
from news_aggregator import NewsAggregator, MarketContext
from market_scanner import MarketInfo

logger = logging.getLogger("bot.ai_engine")

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_MODEL = "claude-sonnet-4-20250514"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MIN_EDGE = 0.08   # 8% minimum edge to signal a trade

# Rate limiting: max API calls per cycle
MAX_EVALUATIONS_PER_CYCLE = 10

# Cache: don't re-evaluate the same market within this window
EVALUATION_CACHE_TTL = 600  # 10 minutes


@dataclass
class ProbabilityEstimate:
    """Result of an AI probability estimation."""
    market_id: str
    question: str
    estimated_probability: float   # 0.0 - 1.0, for the "Yes" / first outcome
    confidence: str                # "high", "medium", "low"
    reasoning: str                 # Claude's explanation
    category: str                  # Market category
    market_price: float            # Current market price for comparison
    edge: float                    # estimated_prob - market_price
    recommended_side: str          # "BUY_YES", "BUY_NO", or "SKIP"
    timestamp: float = field(default_factory=time.time)


# ── Prompt template ───────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert prediction market analyst. Your job is to estimate the true probability of outcomes for prediction market questions.

Rules:
1. Always consider base rates and historical precedent.
2. Do not anchor to the current market price — form your own independent estimate.
3. Account for the time remaining before resolution.
4. Be calibrated: a 70% estimate should resolve "yes" about 70% of the time.
5. Express uncertainty honestly — use "low" confidence when you're unsure.
6. Penalize extreme probabilities (>90% or <10%) — require very strong evidence.
7. Consider what information might change the outcome before resolution.

You must respond with ONLY valid JSON in this exact format:
{
  "probability": 0.XX,
  "confidence": "high/medium/low",
  "reasoning": "Brief explanation of your estimate"
}

The "probability" field is the probability of the FIRST outcome listed (typically "Yes" or "Up").
"""


class AIProbabilityEngine:
    """
    Uses Claude to estimate true probabilities for Polymarket markets.

    Usage:
        engine = AIProbabilityEngine(cfg)
        estimate = engine.evaluate_market(market_info)
        if estimate and estimate.edge > 0.08:
            # Signal a trade
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = os.getenv("AI_MODEL", DEFAULT_MODEL)
        self.min_edge = float(os.getenv("AI_MIN_EDGE", str(DEFAULT_MIN_EDGE)))
        self.news = NewsAggregator()
        self._session = get_session()
        self._cache: Dict[str, ProbabilityEstimate] = {}
        self._cycle_calls: int = 0
        self._cycle_reset_ts: float = 0.0

        if self.api_key:
            logger.info(
                "AI Probability Engine ready | model=%s | min_edge=%.0f%%",
                self.model, self.min_edge * 100,
            )
        else:
            logger.warning(
                "AI Probability Engine disabled: ANTHROPIC_API_KEY not set in .env"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate_market(
        self, market: MarketInfo
    ) -> Optional[ProbabilityEstimate]:
        """
        Estimate the true probability for a market and determine if there's edge.

        Returns:
            ProbabilityEstimate if evaluation succeeds, None on error or skip.
        """
        if not self.enabled:
            return None

        # Rate limit per cycle
        now = time.time()
        if now - self._cycle_reset_ts > 60:
            self._cycle_calls = 0
            self._cycle_reset_ts = now

        if self._cycle_calls >= MAX_EVALUATIONS_PER_CYCLE:
            return None

        # Cache check
        cached = self._cache.get(market.market_id)
        if cached and (now - cached.timestamp) < EVALUATION_CACHE_TTL:
            return cached

        # Get the current market price (yes side)
        yes_token = market.yes_token
        if not yes_token:
            return None
        market_price = yes_token.mid_price or yes_token.best_ask
        if market_price <= 0 or market_price >= 1:
            return None

        # Gather real-world context
        context = self.news.get_context(market.question, market.market_id)

        # Call Claude
        estimate = self._call_claude(market, context, market_price)
        if estimate:
            self._cache[market.market_id] = estimate
            self._cycle_calls += 1

        return estimate

    def evaluate_markets(
        self, markets: List[MarketInfo]
    ) -> List[ProbabilityEstimate]:
        """
        Evaluate multiple markets and return those with significant edge.

        Prioritizes markets that are most likely to have mispricing:
        - Mid-range prices (30-70%) where uncertainty is highest
        - Higher volume (more liquid, easier to trade)
        - Categories where news data is available
        """
        if not self.enabled:
            return []

        # Prioritize markets most likely to have edge
        candidates = self._prioritize_markets(markets)

        estimates = []
        for market in candidates[:MAX_EVALUATIONS_PER_CYCLE]:
            try:
                estimate = self.evaluate_market(market)
                if estimate and abs(estimate.edge) >= self.min_edge:
                    estimates.append(estimate)
                    logger.info(
                        "AI edge found: %s | est=%.1f%% mkt=%.1f%% edge=%+.1f%% [%s] | %s",
                        estimate.recommended_side,
                        estimate.estimated_probability * 100,
                        estimate.market_price * 100,
                        estimate.edge * 100,
                        estimate.confidence,
                        market.question[:60],
                    )
            except Exception as exc:
                logger.debug("AI evaluation failed for %s: %s", market.market_id[:16], exc)

        return estimates

    # ─────────────────────────────────────────────────────────────────────────
    # Claude API call
    # ─────────────────────────────────────────────────────────────────────────

    def _call_claude(
        self,
        market: MarketInfo,
        context: MarketContext,
        market_price: float,
    ) -> Optional[ProbabilityEstimate]:
        """Send a structured prompt to Claude and parse the response."""

        # Build the user message
        outcomes = [t.outcome for t in market.tokens]
        outcomes_str = " / ".join(outcomes) if outcomes else "Yes / No"

        user_msg = f"""Market question: {market.question}

Possible outcomes: {outcomes_str}

Market end date: {market.end_date or 'Unknown'}

Market volume: ${market.volume:,.0f}

Real-world context:
{context.to_prompt_context()}

Based on all available information, what is the true probability of the FIRST outcome ({outcomes[0] if outcomes else 'Yes'})?

Remember: respond with ONLY valid JSON."""

        try:
            resp = self._session.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 300,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                },
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()

        except requests.RequestException as exc:
            logger.warning("Claude API call failed: %s", exc)
            return None

        # Parse response
        try:
            content = result.get("content", [{}])[0].get("text", "")
            # Extract JSON from response (Claude sometimes wraps in markdown)
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if not json_match:
                logger.debug("No JSON found in Claude response: %s", content[:200])
                return None

            parsed = json.loads(json_match.group())
            prob = float(parsed.get("probability", 0.5))
            confidence = parsed.get("confidence", "low")
            reasoning = parsed.get("reasoning", "")

            # Clamp probability
            prob = max(0.02, min(0.98, prob))

            # Calculate edge
            edge = prob - market_price

            # Determine recommended side
            if edge >= self.min_edge:
                recommended = "BUY_YES"
            elif edge <= -self.min_edge:
                recommended = "BUY_NO"
            else:
                recommended = "SKIP"

            return ProbabilityEstimate(
                market_id=market.market_id,
                question=market.question,
                estimated_probability=prob,
                confidence=confidence,
                reasoning=reasoning,
                category=context.category,
                market_price=market_price,
                edge=edge,
                recommended_side=recommended,
            )

        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.debug("Failed to parse Claude response: %s | %s", exc, content[:200])
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Market prioritization
    # ─────────────────────────────────────────────────────────────────────────

    # ─────────────────────────────────────────────────────────────────────────
    # Bayesian re-evaluation of open positions
    # ─────────────────────────────────────────────────────────────────────────

    def reevaluate_position(
        self,
        market: MarketInfo,
        prior_probability: float,
        new_context: Optional[MarketContext] = None,
    ) -> Optional[float]:
        """
        Bayesian update of a previously-estimated probability.

        Implements: P(H|E) = P(E|H) * P(H) / P(E)

        Uses Claude to estimate how likely the new evidence is under
        the hypothesis (H = outcome occurs) vs overall, then applies
        Bayes' rule to update the prior.

        Args:
            market: The market to re-evaluate.
            prior_probability: Our previous probability estimate (0-1).
            new_context: Fresh context; fetched if not provided.

        Returns:
            Updated probability (0-1), or None on failure.
        """
        if not self.enabled:
            return None

        if new_context is None:
            new_context = self.news.get_context(market.question, market.market_id)

        # Build a Bayesian-update prompt
        outcomes = [t.outcome for t in market.tokens]
        outcomes_str = " / ".join(outcomes) if outcomes else "Yes / No"

        user_msg = f"""You previously estimated the probability of "{outcomes[0] if outcomes else 'Yes'}" for this market at {prior_probability:.1%}.

Market question: {market.question}
Possible outcomes: {outcomes_str}
Market end date: {market.end_date or 'Unknown'}

NEW information since your last estimate:
{new_context.to_prompt_context()}

Given this new information, update your probability estimate.
Consider:
1. How likely is this new evidence if the outcome WILL happen? (P(E|H))
2. How likely is this new evidence overall? (P(E))
3. Apply Bayes' rule: P(H|E) = P(E|H) * P(H) / P(E)

Respond with ONLY valid JSON:
{{"updated_probability": 0.XX, "confidence": "high/medium/low", "reasoning": "Brief explanation"}}"""

        try:
            resp = self._session.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 300,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                },
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()

            content = result.get("content", [{}])[0].get("text", "")
            json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
            if not json_match:
                return None

            parsed = json.loads(json_match.group())
            updated = float(parsed.get("updated_probability", prior_probability))
            updated = max(0.02, min(0.98, updated))

            logger.info(
                "Bayesian update for %s: prior=%.1f%% -> posterior=%.1f%% [%s]",
                market.market_id[:16],
                prior_probability * 100,
                updated * 100,
                parsed.get("confidence", "?"),
            )
            return updated

        except Exception as exc:
            logger.debug("Bayesian re-evaluation failed: %s", exc)
            return None

    def _prioritize_markets(self, markets: List[MarketInfo]) -> List[MarketInfo]:
        """
        Rank markets by likelihood of mispricing to optimize API usage.

        Prioritizes:
        - Mid-range prices (30-70%) where disagreement is highest
        - Higher volume (liquid enough to trade)
        - Non-crypto markets (crypto handled by dedicated strategy)
        """
        scored: List[Tuple[float, MarketInfo]] = []

        for market in markets:
            yes_token = market.yes_token
            if not yes_token:
                continue
            price = yes_token.mid_price or yes_token.best_ask
            if price <= 0.05 or price >= 0.95:
                continue  # Extreme prices unlikely to be wrong

            # Skip crypto Up/Down markets (handled by crypto_mean_reversion)
            q = market.question.lower()
            if any(kw in q for kw in ["up or down", "btc up", "eth up", "bitcoin up", "ethereum up"]):
                continue

            # Score: prefer mid-range prices and higher volume
            price_score = 1.0 - abs(price - 0.5) * 2  # Max at 0.50
            volume_score = min(market.volume / 100_000, 1.0)
            total = price_score * 0.6 + volume_score * 0.4

            scored.append((total, market))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored]
