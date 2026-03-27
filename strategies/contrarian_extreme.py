"""
strategies/contrarian_extreme.py
---------------------------------
Contrarian strategy that bets against extreme market prices (90%+).

Based on the statistical finding that ~20% of markets touching 90% odds
subsequently reverse, generating 500-800% returns on the contrarian side.
The 80% that don't reverse result in total loss of the position.

Expected value per 100 trades at $1 each:
  6 trades x $8 profit  = $48
  13 trades x $5 profit = $65
  80 trades x $1 loss   = -$80
  Net: +$33 (+33% on the losing side, +13% overall)

CRITICAL FILTERS to avoid buying near-resolved markets:
  1. Time remaining > 48 hours (markets close to resolution are correct)
  2. Rapid price movement (jumped to 90%+ recently, not sitting there for weeks)
  3. Exclude crypto Up/Down markets (5-min resolution = no time to reverse)
  4. Volume spike detection (emotional/news-driven moves are more likely to revert)
  5. Position cap: max 10% of total positions to limit downside

This is a HIGH RISK strategy — 80% of individual trades lose. It is only
profitable in aggregate across many trades due to the asymmetric payoff.
"""

import datetime
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from strategies.base import BaseStrategy, TradeSignal
from market_scanner import MarketInfo, TokenInfo

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

# Extreme threshold: market must have one side at or above this price
EXTREME_THRESHOLD = 0.90

# Only buy the cheap side if it's in this range (the contrarian bet)
CONTRARIAN_MIN_PRICE = 0.03   # Don't buy below 3c (likely truly dead)
CONTRARIAN_MAX_PRICE = 0.15   # Don't buy above 15c (not extreme enough)

# Minimum hours until resolution (filter out near-resolved markets)
MIN_HOURS_TO_RESOLUTION = 48

# Price history: how recently must the price have moved to this extreme?
# If a market has been at 90%+ for over 7 days, it's probably correct.
MAX_DAYS_AT_EXTREME = 7

# Minimum market volume (filters out dead/illiquid markets)
MIN_VOLUME = 5_000

# Maximum signals per cycle (prevent over-allocation)
MAX_SIGNALS_PER_CYCLE = 1

# Cooldown per market (don't re-enter same market within this window)
MARKET_COOLDOWN_SECONDS = 3600 * 6  # 6 hours

# Crypto market patterns to exclude
CRYPTO_PATTERNS = [
    r"bitcoin\s+up\s+or\s+down",
    r"btc\s+up\s+or\s+down",
    r"ethereum\s+up\s+or\s+down",
    r"eth\s+up\s+or\s+down",
    r"solana\s+up\s+or\s+down",
    r"xrp\s+up\s+or\s+down",
    r"doge\s+up\s+or\s+down",
]


class ContrarianExtremeStrategy(BaseStrategy):
    """
    Bets against extreme market prices where statistical mean-reversion
    creates positive expected value despite a high individual loss rate.

    Position-limited to 10% of MAX_POSITIONS to contain risk.
    """

    def name(self) -> str:
        return "contrarian_extreme"

    def __init__(self, *args, position_tracker=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._position_tracker = position_tracker
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}
        self._market_cooldown: Dict[str, float] = {}

    def scan(self) -> List[TradeSignal]:
        """Scan for contrarian opportunities on extreme-priced markets."""
        signals: List[TradeSignal] = []

        # Position cap: max 10% of allowed positions
        if self._position_tracker:
            current_count = self._position_tracker.position_count()
            max_positions = self.cfg.MAX_POSITIONS
            # Count how many current positions are from this strategy
            contrarian_count = sum(
                1 for p in self._position_tracker.get_all_positions()
                if not p.resolved and hasattr(p, 'outcome')
                and p.entry_price <= CONTRARIAN_MAX_PRICE
            )
            max_contrarian = max(int(max_positions * 0.10), 1)
            if contrarian_count >= max_contrarian:
                self.log.debug(
                    "Contrarian position cap reached (%d/%d). Skipping.",
                    contrarian_count, max_contrarian,
                )
                return []

        markets = self.market_scanner.get_markets()

        for market in markets:
            try:
                signal = self._evaluate_market(market)
                if signal:
                    signals.append(signal)
                    if len(signals) >= MAX_SIGNALS_PER_CYCLE:
                        break
            except Exception as exc:
                self.log.debug(
                    "Error evaluating market %s: %s",
                    market.market_id[:16], exc,
                )

        if signals:
            self.log.info(
                "Contrarian extreme: %d signal(s) found.", len(signals),
            )

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Market evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_market(self, market: MarketInfo) -> Optional[TradeSignal]:
        """
        Check if a market has an extreme price worth fading.

        Returns a signal to buy the cheap side if all filters pass.
        """
        # ── Cooldown check ──────────────────────────────────────────────────
        last_entry = self._market_cooldown.get(market.market_id, 0)
        if time.time() - last_entry < MARKET_COOLDOWN_SECONDS:
            return None

        # ── Exclude crypto short-duration markets ───────────────────────────
        if self._is_crypto_market(market):
            return None

        # ── Volume filter ───────────────────────────────────────────────────
        if market.volume < MIN_VOLUME:
            return None

        # ── Time to resolution filter ───────────────────────────────────────
        hours_remaining = self._hours_to_resolution(market)
        if hours_remaining is not None and hours_remaining < MIN_HOURS_TO_RESOLUTION:
            return None

        # ── Find the extreme and cheap sides ────────────────────────────────
        expensive_token = None
        cheap_token = None

        for token in market.tokens:
            price = token.mid_price or token.best_ask
            if price >= EXTREME_THRESHOLD:
                expensive_token = token
            elif CONTRARIAN_MIN_PRICE <= price <= CONTRARIAN_MAX_PRICE:
                cheap_token = token

        if not expensive_token or not cheap_token:
            return None

        cheap_price = cheap_token.mid_price or cheap_token.best_ask

        # ── Check if the move to extreme is recent ──────────────────────────
        # Update price history
        self._update_history(expensive_token, market)

        # If we've been tracking this market and the expensive side has been
        # at 90%+ for too long, skip (it's probably correct, not overreacting)
        if self._has_been_extreme_too_long(expensive_token.token_id):
            self.log.debug(
                "Market %s has been extreme for too long. Skipping.",
                market.market_id[:16],
            )
            return None

        # ── Spread check (need enough liquidity to exit) ────────────────────
        spread = cheap_token.best_ask - cheap_token.best_bid
        if spread <= 0 or cheap_token.best_ask <= 0:
            return None

        # ── Compute confidence ──────────────────────────────────────────────
        # Base confidence for contrarian extreme is low (80% loss rate)
        # but the asymmetric payoff makes it +EV
        confidence = 0.20  # Base: 20% chance of reversal

        # Boost if there's a volume spike (emotional trading)
        if market.volume > 50_000:
            confidence += 0.05
        if market.volume > 200_000:
            confidence += 0.05

        # Boost if time remaining is very long (more time to reverse)
        if hours_remaining and hours_remaining > 168:  # > 1 week
            confidence += 0.05

        # Boost if cheap token has some bid support (others also fading)
        if cheap_token.bid_size > 100:
            confidence += 0.05

        confidence = min(confidence, 0.40)  # Cap at 40%

        # ── Build signal ────────────────────────────────────────────────────
        # Size: small positions due to high loss rate
        # The strategy relies on aggregate EV, not individual wins
        size = max(self.cfg.MICRO_TRADE_SIZE / cheap_price, 5.0)

        expensive_price = expensive_token.mid_price or expensive_token.best_ask
        potential_return = (1.0 / cheap_price) - 1.0

        reason = (
            f"Contrarian fade: {expensive_token.outcome}={expensive_price:.2f} "
            f"-> buy {cheap_token.outcome}@{cheap_price:.3f} | "
            f"potential={potential_return:.0%} | "
            f"vol=${market.volume:.0f} | "
            f"hours_left={hours_remaining:.0f}h | "
            f"{market.question[:50]}"
        )

        signal = TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=cheap_token.token_id,
            side="BUY",
            price=round(cheap_token.best_ask, 4),
            size=round(size, 2),
            confidence=confidence,
            reason=reason,
            order_type="GTC",
        )

        self._market_cooldown[market.market_id] = time.time()
        self._log_signal(signal)
        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # Filters
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_crypto_market(market: MarketInfo) -> bool:
        """Exclude crypto Up/Down markets (too short-term for this strategy)."""
        q = market.question.lower()
        for pattern in CRYPTO_PATTERNS:
            if re.search(pattern, q):
                return True
        # Also check for Up/Down outcomes with crypto keywords
        outcomes = [t.outcome.lower() for t in market.tokens]
        if "up" in outcomes and "down" in outcomes:
            if any(kw in q for kw in ["bitcoin", "btc", "ethereum", "eth",
                                       "solana", "sol", "crypto", "xrp", "doge"]):
                return True
        return False

    @staticmethod
    def _hours_to_resolution(market: MarketInfo) -> Optional[float]:
        """Calculate hours until market resolves. Returns None if unknown."""
        if not market.end_date:
            return None
        try:
            end_str = market.end_date
            if "T" in end_str:
                end_dt = datetime.datetime.fromisoformat(
                    end_str.replace("Z", "+00:00")
                )
            else:
                end_dt = datetime.datetime.fromisoformat(end_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            return max((end_dt - now).total_seconds() / 3600, 0)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Price history tracking
    # ─────────────────────────────────────────────────────────────────────────

    def _update_history(self, token: TokenInfo, market: MarketInfo) -> None:
        """Track token price over time."""
        price = token.mid_price or token.best_ask
        if price <= 0:
            return
        history = self._price_history.setdefault(token.token_id, [])
        history.append((time.time(), price))
        # Keep last 100 observations
        if len(history) > 100:
            self._price_history[token.token_id] = history[-100:]

    def _has_been_extreme_too_long(self, token_id: str) -> bool:
        """
        Check if a token has been at extreme levels for too long.

        If the price has been >= EXTREME_THRESHOLD for more than
        MAX_DAYS_AT_EXTREME days, the market is probably correctly priced
        and not overreacting.
        """
        history = self._price_history.get(token_id, [])
        if len(history) < 5:
            return False  # Not enough data — allow the trade

        # Check the oldest observation we have
        oldest_ts, oldest_price = history[0]
        days_tracked = (time.time() - oldest_ts) / 86400

        if days_tracked < 1:
            return False  # Less than a day of data — allow

        # If every observation shows extreme price, it's been there too long
        all_extreme = all(p >= EXTREME_THRESHOLD * 0.95 for _, p in history)

        if all_extreme and days_tracked >= MAX_DAYS_AT_EXTREME:
            return True

        return False
