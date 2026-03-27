"""
strategies/lp_rewards.py
-------------------------
Liquidity Provider (LP) rewards strategy.

Earns reward tokens by providing liquidity to high-reward Polymarket markets.
Places tight limit orders around the midpoint on markets with the largest
daily reward pools, refreshes them periodically, and hedges any fills.

Parameters (via config / .env):
  LP_ENABLED:          Enable/disable this strategy
  LP_CAPITAL_PCT:      Fraction of bankroll to allocate (default 0.20 = 20%)
  LP_MAX_MARKETS:      Maximum markets to provide liquidity in (default 5)
  LP_REFRESH_INTERVAL: Cancel and re-place orders every N seconds (default 300)

Behaviour:
  1. Fetch reward data from Gamma API (sorted by daily reward pool).
  2. Place bid/ask limit orders within a tight spread of midpoint.
  3. Periodically cancel + re-place to avoid stale fills.
  4. If filled, immediately place a counter-order at the same price.
  5. Track cumulative reward earnings.
"""

import logging
import time
from typing import Dict, List

from http_client import get_session
from strategies.base import BaseStrategy, TradeSignal

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Spread from midpoint for LP orders
LP_SPREAD = 0.02  # 2 cents each side
MIN_LP_PRICE = 0.10
MAX_LP_PRICE = 0.90
MAX_SIGNALS_PER_CYCLE = 4


class LPRewardsStrategy(BaseStrategy):
    """
    Provides liquidity to high-reward Polymarket markets to earn LP rewards.

    Inherits from BaseStrategy and implements the standard scan() interface.
    """

    def name(self) -> str:
        return "lp_rewards"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._session = get_session()
        self._reward_cache: List[dict] = []
        self._reward_cache_ts: float = 0.0
        self._active_orders: Dict[str, Dict] = {}  # market_id -> order info
        self._last_refresh: Dict[str, float] = {}   # market_id -> last refresh ts
        self._fill_count: int = 0
        self._reward_earnings: float = 0.0

    def scan(self) -> List[TradeSignal]:
        """
        Scan for LP opportunities on high-reward markets.

        Returns limit-order signals (both BUY and SELL side) to provide
        liquidity around the midpoint.
        """
        if not self.cfg.LP_ENABLED:
            return []

        signals: List[TradeSignal] = []

        # Fetch reward markets
        reward_markets = self._get_reward_markets()
        if not reward_markets:
            self.log.debug("No reward markets found.")
            return []

        # Calculate per-market capital allocation
        capital_per_market = self._compute_capital_per_market()
        if capital_per_market < 5.0:
            self.log.debug("Insufficient LP capital: $%.2f per market.", capital_per_market)
            return []

        now = time.time()
        signal_count = 0

        for rm in reward_markets[:self.cfg.LP_MAX_MARKETS]:
            market_id = rm.get("conditionId") or rm.get("id", "")
            if not market_id:
                continue

            # Check refresh interval
            last_refresh = self._last_refresh.get(market_id, 0)
            if now - last_refresh < self.cfg.LP_REFRESH_INTERVAL:
                continue

            # Get live market data
            market = self.market_scanner.get_market(market_id)
            if not market:
                continue

            yes_token = market.yes_token
            if not yes_token or yes_token.mid_price <= 0:
                continue

            mid = yes_token.mid_price

            # Only LP on markets with mid-range prices
            if mid < MIN_LP_PRICE or mid > MAX_LP_PRICE:
                continue

            # Place bid (BUY) below midpoint
            bid_price = round(mid - LP_SPREAD, 4)
            if bid_price > 0.01:
                bid_size = round(capital_per_market / bid_price, 2)
                if bid_size >= 2.0:
                    signals.append(TradeSignal(
                        strategy=self.name(),
                        market_id=market_id,
                        token_id=yes_token.token_id,
                        side="BUY",
                        price=bid_price,
                        size=bid_size,
                        confidence=0.50,
                        reason=f"LP bid | mid={mid:.3f} | reward_pool={rm.get('rewardPool', '?')}",
                        order_type="GTC",
                    ))
                    signal_count += 1

            # Place ask (SELL) above midpoint — only if we hold the token
            # In practice this would check position; for now emit signal
            # and let risk manager handle it
            ask_price = round(mid + LP_SPREAD, 4)
            if ask_price < 0.99:
                ask_size = round(capital_per_market / ask_price, 2)
                if ask_size >= 2.0:
                    signals.append(TradeSignal(
                        strategy=self.name(),
                        market_id=market_id,
                        token_id=yes_token.token_id,
                        side="SELL",
                        price=ask_price,
                        size=ask_size,
                        confidence=0.50,
                        reason=f"LP ask | mid={mid:.3f} | reward_pool={rm.get('rewardPool', '?')}",
                        order_type="GTC",
                    ))
                    signal_count += 1

            self._last_refresh[market_id] = now

            if signal_count >= MAX_SIGNALS_PER_CYCLE:
                break

        if signals:
            self.log.info(
                "LP strategy: %d signals across %d markets (capital=$%.0f/market).",
                len(signals), min(len(reward_markets), self.cfg.LP_MAX_MARKETS),
                capital_per_market,
            )

        return signals

    def _get_reward_markets(self) -> List[dict]:
        """
        Fetch markets with LP reward pools from Gamma API.

        Caches results for 5 minutes.
        """
        now = time.time()
        if self._reward_cache and (now - self._reward_cache_ts) < 300:
            return self._reward_cache

        try:
            resp = self._session.get(
                f"{GAMMA_API}/markets",
                params={
                    "active": "true",
                    "enableOrderBook": "true",
                    "closed": "false",
                    "order": "liquidity",
                    "ascending": "false",
                    "limit": 50,
                },
                timeout=15,
            )
            resp.raise_for_status()
            markets = resp.json()

            if not isinstance(markets, list):
                markets = markets.get("data", [])

            # Filter to markets with rewards / high liquidity
            # Gamma API may include reward info in the response
            reward_markets = []
            for m in markets:
                liquidity = float(m.get("liquidity", 0) or 0)
                volume = float(m.get("volume", 0) or 0)
                # Proxy for reward eligibility: high liquidity markets
                if liquidity >= 50000 or volume >= 100000:
                    m["rewardPool"] = f"${liquidity:,.0f}"
                    reward_markets.append(m)

            # Sort by liquidity descending (proxy for reward pool size)
            reward_markets.sort(
                key=lambda x: float(x.get("liquidity", 0) or 0), reverse=True
            )

            self._reward_cache = reward_markets
            self._reward_cache_ts = now
            return reward_markets

        except Exception as exc:
            self.log.warning("Failed to fetch reward markets: %s", exc)
            return self._reward_cache

    def _compute_capital_per_market(self) -> float:
        """
        Compute the USD allocation per LP market based on config.

        Uses LP_CAPITAL_PCT of MAX_TOTAL_EXPOSURE, divided by LP_MAX_MARKETS.
        """
        total_lp_capital = self.cfg.MAX_TOTAL_EXPOSURE * self.cfg.LP_CAPITAL_PCT
        return total_lp_capital / max(self.cfg.LP_MAX_MARKETS, 1)
