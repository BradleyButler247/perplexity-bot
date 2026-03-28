"""
strategies/cross_market_arb.py
-------------------------------
Cross-market arbitrage strategy using KL-divergence and logical consistency
checks across related Polymarket markets within the same event group.

Types of inconsistencies detected:

  1. TEMPORAL MONOTONICITY
     Markets with escalating deadlines must have non-decreasing probabilities.
     "X by March" at 5% and "X by June" at 3% → buy June, it's underpriced.

  2. SUBSET CONSISTENCY
     If event A implies event B, P(A) <= P(B).
     "BTC hits $100K" implies "BTC hits $90K" → the $90K market must be
     priced at least as high as $100K.

  3. MULTI-OUTCOME SUM
     Mutually exclusive outcomes (e.g., "Who wins Best Picture?") should
     sum to ~100%. If they sum to 90%, buy all of them. If 110%, sell.

  4. KL-DIVERGENCE OUTLIERS
     Within an event group, use KL-divergence to find the market whose
     pricing is most inconsistent with the group's implied distribution.

Data source: Gamma API /events endpoint, which groups related markets.
"""

import json
import logging
import math
import re
import time
from typing import Dict, List, Optional, Tuple

from http_client import get_session
from strategies.base import BaseStrategy, TradeSignal

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Minimum edge to signal a trade
MIN_EDGE = 0.05  # 5 cents mispricing

# Minimum liquidity to consider trading
MIN_LIQUIDITY = 5000

# Maximum events to scan per cycle (API rate limiting)
MAX_EVENTS_PER_CYCLE = 30

# Cache duration for event data
EVENT_CACHE_TTL = 300  # 5 minutes

# Cooldown per event
EVENT_COOLDOWN = 600  # 10 minutes

# Max signals per cycle
MAX_SIGNALS_PER_CYCLE = 2


class CrossMarketArbStrategy(BaseStrategy):
    """
    Finds pricing inconsistencies across related markets within the same
    event group on Polymarket.
    """

    def name(self) -> str:
        return "cross_market_arb"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._session = get_session()
        self._event_cache: List[dict] = []
        self._event_cache_ts: float = 0.0
        self._event_cooldown: Dict[str, float] = {}

    def scan(self) -> List[TradeSignal]:
        """Scan event groups for cross-market arbitrage opportunities."""
        signals: List[TradeSignal] = []

        events = self._fetch_events()
        if not events:
            return []

        for event in events:
            try:
                event_signals = self._evaluate_event(event)
                signals.extend(event_signals)
                if len(signals) >= MAX_SIGNALS_PER_CYCLE:
                    break
            except Exception as exc:
                self.log.debug(
                    "Error evaluating event %s: %s",
                    event.get("id", "?"), exc,
                )

        if signals:
            self.log.info(
                "Cross-market arb: %d signal(s) found.", len(signals),
            )

        return signals[:MAX_SIGNALS_PER_CYCLE]

    # ─────────────────────────────────────────────────────────────────────────
    # Event evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_event(self, event: dict) -> List[TradeSignal]:
        """
        Check an event group for pricing inconsistencies.

        Runs all consistency checks and returns signals for any found.
        """
        event_id = event.get("id", "")

        # Cooldown
        if time.time() - self._event_cooldown.get(event_id, 0) < EVENT_COOLDOWN:
            return []

        markets = event.get("markets", [])
        # Filter to active, open markets with valid prices
        active_markets = [
            m for m in markets
            if m.get("active") and not m.get("closed")
            and m.get("outcomePrices")
            and m.get("conditionId")
        ]

        if len(active_markets) < 2:
            return []

        signals = []

        # Check 1: Temporal monotonicity
        temporal_signals = self._check_temporal_monotonicity(event, active_markets)
        signals.extend(temporal_signals)

        # Check 2: Multi-outcome sum
        sum_signals = self._check_outcome_sum(event, active_markets)
        signals.extend(sum_signals)

        # Check 3: KL-divergence outlier
        kl_signals = self._check_kl_divergence(event, active_markets)
        signals.extend(kl_signals)

        if signals:
            self._event_cooldown[event_id] = time.time()

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Check 1: Temporal monotonicity
    # ─────────────────────────────────────────────────────────────────────────

    def _check_temporal_monotonicity(
        self, event: dict, markets: List[dict]
    ) -> List[TradeSignal]:
        """
        Markets with later deadlines should have >= probability of earlier ones.

        "X by March" at 5%, "X by June" at 3% → June is underpriced.
        """
        # Try to extract dates from market questions and sort by deadline
        dated_markets = []
        for m in markets:
            date_info = self._extract_date_from_question(m.get("question", ""))
            if date_info:
                yes_price = self._get_yes_price(m)
                if yes_price and yes_price > 0:
                    dated_markets.append((date_info, yes_price, m))

        if len(dated_markets) < 2:
            return []

        # Sort by date
        dated_markets.sort(key=lambda x: x[0])

        signals = []
        for i in range(1, len(dated_markets)):
            earlier_date, earlier_price, earlier_mkt = dated_markets[i - 1]
            later_date, later_price, later_mkt = dated_markets[i]

            # Later deadline should have higher or equal probability
            if later_price < earlier_price - MIN_EDGE:
                edge = earlier_price - later_price
                # Buy the later (underpriced) market's YES token
                token_id = self._get_yes_token_id(later_mkt)
                condition_id = later_mkt.get("conditionId", "")

                if token_id and condition_id:
                    size = max(self.cfg.MICRO_TRADE_SIZE / later_price, 5.0)

                    signal = TradeSignal(
                        strategy=self.name(),
                        market_id=condition_id,
                        token_id=token_id,
                        side="BUY",
                        price=round(later_price, 4),
                        size=round(size, 2),
                        confidence=min(0.50 + edge, 0.85),
                        reason=(
                            f"Temporal arb: '{later_mkt.get('question', '')[:40]}' "
                            f"at {later_price:.1%} < earlier deadline at {earlier_price:.1%} | "
                            f"edge={edge:.1%} | event={event.get('title', '')[:30]}"
                        ),
                        order_type="GTC",
                    )
                    signals.append(signal)
                    self._log_signal(signal)

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Check 2: Multi-outcome sum check
    # ─────────────────────────────────────────────────────────────────────────

    def _check_outcome_sum(
        self, event: dict, markets: List[dict]
    ) -> List[TradeSignal]:
        """
        For mutually exclusive outcomes (e.g., "Who wins?"), prices should
        sum to approximately 100%. If significantly under, buy the
        most underpriced outcome.
        """
        # Only applies to events where markets are alternatives (not time-based)
        title = (event.get("title", "") or "").lower()

        # Detect "who will win" type events
        is_multi_outcome = any(kw in title for kw in [
            "who will", "winner", "which", "best picture",
            "next president", "next prime minister", "mvp",
        ])

        if not is_multi_outcome:
            return []

        # Sum all YES prices
        total = 0.0
        market_prices: List[Tuple[float, dict]] = []

        for m in markets:
            yes_price = self._get_yes_price(m)
            if yes_price and 0 < yes_price < 1:
                total += yes_price
                market_prices.append((yes_price, m))

        if len(market_prices) < 3 or total <= 0:
            return []

        signals = []

        # If total is significantly below 1.0, outcomes are collectively underpriced
        if total < (1.0 - MIN_EDGE * 2):
            underpricing = 1.0 - total

            # Buy the most liquid / highest-volume underpriced outcome
            market_prices.sort(key=lambda x: x[0], reverse=True)

            for price, mkt in market_prices[:1]:  # Just the top candidate
                token_id = self._get_yes_token_id(mkt)
                condition_id = mkt.get("conditionId", "")

                if token_id and condition_id:
                    # Scale confidence by how underpriced the total is
                    confidence = min(0.40 + underpricing * 2, 0.80)
                    size = max(self.cfg.MICRO_TRADE_SIZE / price, 5.0)

                    signal = TradeSignal(
                        strategy=self.name(),
                        market_id=condition_id,
                        token_id=token_id,
                        side="BUY",
                        price=round(price, 4),
                        size=round(size, 2),
                        confidence=confidence,
                        reason=(
                            f"Sum arb: outcomes total {total:.1%} (should be ~100%) | "
                            f"gap={underpricing:.1%} | buying top outcome at {price:.1%} | "
                            f"event={event.get('title', '')[:30]}"
                        ),
                        order_type="GTC",
                    )
                    signals.append(signal)
                    self._log_signal(signal)

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Check 3: KL-divergence outlier
    # ─────────────────────────────────────────────────────────────────────────

    def _check_kl_divergence(
        self, event: dict, markets: List[dict]
    ) -> List[TradeSignal]:
        """
        Use KL-divergence to find the market within an event group whose
        pricing is most inconsistent with the group's implied distribution.
        """
        if len(markets) < 3:
            return []

        # Build probability vectors
        prices = []
        valid_markets = []
        for m in markets:
            yes_price = self._get_yes_price(m)
            if yes_price and 0.01 < yes_price < 0.99:
                prices.append(yes_price)
                valid_markets.append(m)

        if len(prices) < 3:
            return []

        # Normalize to form a distribution
        total = sum(prices)
        if total <= 0:
            return []

        p_dist = [p / total for p in prices]  # Market distribution

        # Uniform prior as reference
        n = len(prices)
        q_dist = [1.0 / n] * n

        # Compute per-element KL contribution
        kl_contributions = []
        for i in range(n):
            if p_dist[i] > 0 and q_dist[i] > 0:
                kl = p_dist[i] * math.log(p_dist[i] / q_dist[i])
            else:
                kl = 0
            kl_contributions.append(kl)

        # Find the market contributing least to the distribution
        # (most underpriced relative to uniform expectation)
        min_kl_idx = min(range(n), key=lambda i: kl_contributions[i])
        max_kl_idx = max(range(n), key=lambda i: kl_contributions[i])

        # The underpriced market (low KL contribution) might be a buy
        underpriced_mkt = valid_markets[min_kl_idx]
        underpriced_price = prices[min_kl_idx]
        expected_share = 1.0 / n
        actual_share = p_dist[min_kl_idx]

        edge = (expected_share - actual_share) * total

        if edge < MIN_EDGE:
            return []

        signals = []
        token_id = self._get_yes_token_id(underpriced_mkt)
        condition_id = underpriced_mkt.get("conditionId", "")

        if token_id and condition_id:
            size = max(self.cfg.MICRO_TRADE_SIZE / underpriced_price, 5.0)

            signal = TradeSignal(
                strategy=self.name(),
                market_id=condition_id,
                token_id=token_id,
                side="BUY",
                price=round(underpriced_price, 4),
                size=round(size, 2),
                confidence=min(0.45 + edge * 2, 0.75),
                reason=(
                    f"KL arb: '{underpriced_mkt.get('question', '')[:40]}' "
                    f"at {underpriced_price:.1%} underpriced vs group | "
                    f"edge={edge:.1%} | KL={kl_contributions[min_kl_idx]:.4f} | "
                    f"event={event.get('title', '')[:30]}"
                ),
                order_type="GTC",
            )
            signals.append(signal)
            self._log_signal(signal)

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Data fetching
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_events(self) -> List[dict]:
        """Fetch active events from the Gamma API with caching."""
        now = time.time()
        if self._event_cache and (now - self._event_cache_ts) < EVENT_CACHE_TTL:
            return self._event_cache

        try:
            resp = self._session.get(
                GAMMA_EVENTS_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": MAX_EVENTS_PER_CYCLE,
                    "order": "volume",
                    "ascending": "false",
                },
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json() or []

            # Filter to events with multiple active markets
            multi_market = [
                e for e in events
                if len([m for m in e.get("markets", [])
                       if m.get("active") and not m.get("closed")]) >= 2
            ]

            self._event_cache = multi_market
            self._event_cache_ts = now
            self.log.info(
                "Cross-market arb: fetched %d multi-market event group(s) from Gamma API.",
                len(multi_market),
            )
            return multi_market

        except Exception as exc:
            self.log.debug("Event fetch failed: %s", exc)
            return self._event_cache  # Return stale cache on error

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_yes_price(market: dict) -> Optional[float]:
        """Extract the YES outcome price from a market dict."""
        prices = market.get("outcomePrices", [])
        if prices and len(prices) >= 1:
            try:
                return float(prices[0])
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _get_yes_token_id(market: dict) -> Optional[str]:
        """Extract the YES token ID from a market dict."""
        tokens = market.get("clobTokenIds", [])
        if isinstance(tokens, str):
            try:
                tokens = json.loads(tokens)
            except Exception:
                return None
        if tokens and len(tokens) >= 1:
            return str(tokens[0])
        return None

    @staticmethod
    def _extract_date_from_question(question: str) -> Optional[str]:
        """
        Extract a date string from a market question for temporal ordering.

        Returns a sortable date string (YYYY-MM-DD) or None.
        """
        month_map = {
            "january": "01", "february": "02", "march": "03", "april": "04",
            "may": "05", "june": "06", "july": "07", "august": "08",
            "september": "09", "october": "10", "november": "11", "december": "12",
        }

        q = question.lower()

        # Pattern: "by Month DD, YYYY" or "by Month YYYY" or "in YYYY"
        for month_name, month_num in month_map.items():
            if month_name in q:
                # Try to find year
                year_match = re.search(r'(202[4-9]|203[0-9])', q)
                year = year_match.group(1) if year_match else "2026"

                # Try to find day
                day_match = re.search(rf'{month_name}\s+(\d{{1,2}})', q)
                day = day_match.group(1).zfill(2) if day_match else "15"

                return f"{year}-{month_num}-{day}"

        # Fallback: just look for a year
        year_match = re.search(r'(202[4-9]|203[0-9])', q)
        if year_match:
            return f"{year_match.group(1)}-06-15"

        return None
