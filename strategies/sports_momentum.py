"""
strategies/sports_momentum.py
------------------------------
Detects sudden price/volume movements on sports prediction markets and
rides the momentum before the move fully completes.

The premise: when a goal, penalty, red card, or other key event happens
in a live match, the Polymarket price starts moving as informed traders
react. This strategy detects the early phase of that move by monitoring:

  1. Price velocity — how fast a token's price is changing
  2. Volume spike — sudden increase in trading activity
  3. Order book shift — bid/ask imbalance changing rapidly

When all three align, the bot buys in the direction of the move,
betting that the price hasn't finished adjusting yet.

This won't front-run the market (that requires premium data feeds),
but it can catch the middle and tail end of 15-30 point moves that
take 20-60 seconds to fully price in.

Target markets: any sports/esports market with live events in progress.
"""

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from strategies.base import BaseStrategy, TradeSignal
from market_scanner import MarketInfo, TokenInfo

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

# Minimum price velocity to trigger (cents per observation cycle)
# A 5-cent move in one cycle (~10-15 seconds) suggests a live event
MIN_PRICE_VELOCITY = 0.03  # 3 cents per cycle

# Volume spike: current cycle volume must be this multiple of average
VOLUME_SPIKE_RATIO = 3.0

# Only enter tokens in this price range (avoid extremes)
MIN_ENTRY_PRICE = 0.10
MAX_ENTRY_PRICE = 0.85

# Maximum signals per cycle
MAX_SIGNALS_PER_CYCLE = 2

# Cooldown per market (avoid chasing the same event)
MARKET_COOLDOWN = 180  # 3 minutes

# Sports/esports market detection patterns
SPORTS_PATTERNS = [
    r"\b(win|beat|defeat|advance|qualify|champion)\b",
    r"\b(nba|nfl|mlb|nhl|mls|premier league|la liga|serie a|champions league|europa)\b",
    r"\b(match|game|bout|fight|race|tournament|playoff|final|semifinal)\b",
    r"\b(real madrid|barcelona|liverpool|manchester|arsenal|chelsea|juventus|bayern|psg)\b",
    r"\b(lakers|celtics|warriors|nets|knicks|76ers|bucks|nuggets|heat)\b",
    r"\b(chiefs|eagles|49ers|cowboys|bills|ravens|lions|packers)\b",
    r"\b(yankees|dodgers|braves|astros|phillies|mets|padres)\b",
    r"\b(league of legends|lol|cs2|valorant|dota|overwatch|worlds|major)\b",
    r"\b(ufc|mma|boxing|f1|formula|nascar|tennis|golf|pga)\b",
]

# Exclude crypto markets
CRYPTO_EXCLUDE = [
    r"bitcoin|btc|ethereum|eth|solana|sol|crypto|up or down",
]


class SportsMomentumStrategy(BaseStrategy):
    """
    Detects and rides momentum on live sports prediction markets.

    Monitors price velocity and volume spikes to catch in-progress
    market moves from live events (goals, penalties, upsets, etc.)
    """

    def name(self) -> str:
        return "sports_momentum"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # price history: token_id -> list of (timestamp, mid_price)
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}
        # volume history: market_id -> list of (timestamp, volume)
        self._volume_history: Dict[str, List[Tuple[float, float]]] = {}
        self._market_cooldown: Dict[str, float] = {}

    def scan(self) -> List[TradeSignal]:
        """Scan sports markets for momentum signals."""
        signals: List[TradeSignal] = []
        markets = self.market_scanner.get_markets()

        # Filter to sports/esports markets
        sports_markets = [m for m in markets if self._is_sports_market(m)]

        if not sports_markets:
            self.log.debug("No active sports markets found.")
            return []

        for market in sports_markets:
            try:
                # Update history for all tokens (needed for velocity calc)
                self._update_histories(market)

                market_signals = self._evaluate_market(market)
                signals.extend(market_signals)

                if len(signals) >= MAX_SIGNALS_PER_CYCLE:
                    break
            except Exception as exc:
                self.log.debug(
                    "Error evaluating sports market %s: %s",
                    market.market_id[:16], exc,
                )

        if signals:
            self.log.info(
                "Sports momentum: %d signal(s) from %d market(s).",
                len(signals), len(sports_markets),
            )

        return signals[:MAX_SIGNALS_PER_CYCLE]

    # ─────────────────────────────────────────────────────────────────────────
    # Market identification
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_sports_market(market: MarketInfo) -> bool:
        """Check if a market is sports/esports related."""
        q = market.question.lower()

        # Exclude crypto
        for pattern in CRYPTO_EXCLUDE:
            if re.search(pattern, q):
                return False

        # Check for sports patterns
        for pattern in SPORTS_PATTERNS:
            if re.search(pattern, q):
                return True

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # History tracking
    # ─────────────────────────────────────────────────────────────────────────

    def _update_histories(self, market: MarketInfo) -> None:
        """Update price and volume history for a market."""
        now = time.time()

        # Volume history
        vol_history = self._volume_history.setdefault(market.market_id, [])
        vol_history.append((now, market.volume))
        if len(vol_history) > 30:
            self._volume_history[market.market_id] = vol_history[-30:]

        # Price history per token
        for token in market.tokens:
            price = token.mid_price or token.best_bid
            if price <= 0:
                continue
            history = self._price_history.setdefault(token.token_id, [])
            history.append((now, price))
            if len(history) > 30:
                self._price_history[token.token_id] = history[-30:]

    # ─────────────────────────────────────────────────────────────────────────
    # Market evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_market(self, market: MarketInfo) -> List[TradeSignal]:
        """Check if a sports market is experiencing momentum."""
        # Cooldown
        if time.time() - self._market_cooldown.get(market.market_id, 0) < MARKET_COOLDOWN:
            return []

        # Need at least a few observations to detect velocity
        vol_history = self._volume_history.get(market.market_id, [])
        if len(vol_history) < 3:
            return []

        # Check for volume spike
        volume_spike = self._detect_volume_spike(market.market_id)

        signals = []
        for token in market.tokens:
            signal = self._evaluate_token(token, market, volume_spike)
            if signal:
                signals.append(signal)
                self._market_cooldown[market.market_id] = time.time()
                break  # Only one signal per market per cycle

        return signals

    def _evaluate_token(
        self, token: TokenInfo, market: MarketInfo, volume_spike: float
    ) -> Optional[TradeSignal]:
        """
        Check if a token is experiencing price momentum worth riding.
        """
        price = token.mid_price or token.best_ask
        if price < MIN_ENTRY_PRICE or price > MAX_ENTRY_PRICE:
            return None

        # Calculate price velocity
        velocity = self._get_price_velocity(token.token_id)
        if velocity is None:
            return None

        abs_velocity = abs(velocity)

        # Need minimum velocity to trigger
        if abs_velocity < MIN_PRICE_VELOCITY:
            return None

        # Direction: positive velocity = price rising (buy this token)
        # negative velocity = price falling (skip — or buy the other side)
        if velocity <= 0:
            return None  # Only buy tokens that are moving UP

        # ── Confidence scoring ──────────────────────────────────────────────
        confidence = 0.0

        # Velocity strength (stronger move = higher confidence)
        velocity_score = min(abs_velocity / 0.10, 1.0)  # Saturates at 10c/cycle
        confidence += velocity_score * 0.35

        # Volume spike confirmation
        if volume_spike >= VOLUME_SPIKE_RATIO:
            confidence += 0.25
        elif volume_spike >= 2.0:
            confidence += 0.15

        # Price position (mid-range preferred — more room to move)
        if 0.25 <= price <= 0.75:
            confidence += 0.15
        elif 0.15 <= price <= 0.85:
            confidence += 0.10

        # Spread check (tighter = more liquid = better fills)
        spread = token.best_ask - token.best_bid if token.best_ask > 0 and token.best_bid > 0 else 1.0
        if spread < 0.03:
            confidence += 0.10
        elif spread < 0.05:
            confidence += 0.05

        # Order book imbalance (more bids than asks = buying pressure)
        if token.bid_size > 0 and token.ask_size > 0:
            imbalance = token.bid_size / (token.bid_size + token.ask_size)
            if imbalance > 0.6:
                confidence += 0.10

        confidence = min(confidence, 0.90)

        # Need reasonable confidence
        if confidence < 0.40:
            return None

        # ── Build signal ────────────────────────────────────────────────────
        size = max(self.cfg.MAX_POSITION_SIZE * confidence * 0.3 / price, 5.0)

        reason = (
            f"Sports momentum: {token.outcome} velocity={velocity:+.3f}/cycle | "
            f"vol_spike={volume_spike:.1f}x | "
            f"conf={confidence:.2f} | "
            f"{market.question[:60]}"
        )

        signal = TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=token.token_id,
            side="BUY",
            price=round(token.best_ask, 4),
            size=round(size, 2),
            confidence=confidence,
            reason=reason,
            order_type="GTC",
        )

        self._log_signal(signal)
        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # Indicator computations
    # ─────────────────────────────────────────────────────────────────────────

    def _get_price_velocity(self, token_id: str) -> Optional[float]:
        """
        Calculate the rate of price change for a token.

        Returns price change per observation cycle (positive = rising).
        Returns None if not enough data.
        """
        history = self._price_history.get(token_id, [])
        if len(history) < 3:
            return None

        # Use last 3 observations for short-term velocity
        recent = history[-3:]
        oldest_price = recent[0][1]
        newest_price = recent[-1][1]
        time_span = recent[-1][0] - recent[0][0]

        if time_span <= 0 or oldest_price <= 0:
            return None

        # Price change per cycle (normalize by number of observations)
        velocity = (newest_price - oldest_price) / len(recent)

        return velocity

    def _detect_volume_spike(self, market_id: str) -> float:
        """
        Detect if current volume is significantly above the recent average.

        Returns the ratio of current volume rate to average volume rate.
        A ratio of 3.0 means volume is 3x the normal rate.
        """
        history = self._volume_history.get(market_id, [])
        if len(history) < 5:
            return 1.0

        # Calculate volume increments (how much volume was added each cycle)
        increments = []
        for i in range(1, len(history)):
            vol_delta = history[i][1] - history[i-1][1]
            time_delta = history[i][0] - history[i-1][0]
            if time_delta > 0 and vol_delta >= 0:
                increments.append(vol_delta / time_delta)  # Volume per second

        if len(increments) < 3:
            return 1.0

        # Compare latest increment to average
        avg_rate = sum(increments[:-1]) / len(increments[:-1])
        latest_rate = increments[-1]

        if avg_rate <= 0:
            return 1.0 if latest_rate <= 0 else 5.0  # Spike from zero

        return latest_rate / avg_rate
