"""
strategies/signal_based.py
--------------------------
Signal / value composite strategy.

This strategy scores markets across multiple independent signals, combines
them into a composite score, and generates limit-order BUY signals when the
composite score exceeds SIGNAL_MIN_EDGE.

Signals used:
  1. Volume spike    — Recent volume significantly above recent average.
  2. Price momentum  — Strong directional price movement (trending).
  3. Value detection — Price in a low-probability range (0.05–0.30) but with
                       meaningful volume, suggesting potential mispricing.
  4. Spread analysis — Wide bid/ask spread relative to midpoint indicates
                       dislocated market / potential opportunity.

Each signal contributes a normalised score in [0, 1].  The final composite
is a weighted average.  Only trades where composite > SIGNAL_MIN_EDGE are
signalled.

Orders are placed as GTC limit orders at slightly better than the current ask
(aggressive but not a market order) to capture spread while still filling
quickly in a trending market.
"""

import logging
import time
from typing import Dict, List, Optional

from strategies.base import BaseStrategy, TradeSignal
from market_scanner import MarketInfo, TokenInfo

logger = logging.getLogger(__name__)

# ── Signal weights (must sum to 1.0) ─────────────────────────────────────────
WEIGHT_VOLUME_SPIKE = 0.30
WEIGHT_MOMENTUM = 0.25
WEIGHT_VALUE = 0.25
WEIGHT_SPREAD = 0.20

# ── Tuning parameters ────────────────────────────────────────────────────────
VOLUME_SPIKE_RATIO = 1.5        # volume must be 2× the stored baseline
VALUE_PRICE_LOW = 0.05          # "underdog" price range
VALUE_PRICE_HIGH = 0.65
VALUE_MIN_VOLUME = 1_000.0      # market must have real activity
SPREAD_THRESHOLD_WIDE = 0.02    # spread > 2 cents = "wide"
MOMENTUM_THRESHOLD = 0.015      # 1.5-cent directional move = "strong"

# Limit price offset: place order this many cents ABOVE current ask
LIMIT_ABOVE_ASK = 0.01

# Minimum order size in shares
MIN_SHARES = 2.0


class SignalBasedStrategy(BaseStrategy):
    """
    Multi-signal value/momentum strategy using GTC limit orders.

    Maintains a lightweight history of mid-prices to detect momentum.
    History is stored in memory only; it resets on bot restart.
    """

    def name(self) -> str:
        return "signal_based"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # token_id -> list of (timestamp, mid_price) tuples
        self._price_history: Dict[str, list] = {}
        # token_id -> baseline volume (set on first observation)
        self._volume_baseline: Dict[str, float] = {}

    def scan(self) -> List[TradeSignal]:
        """
        Evaluate all markets and return signals for high-scoring ones.

        Returns:
            List of TradeSignal objects.
        """
        signals: List[TradeSignal] = []
        markets = self.market_scanner.get_markets()

        for market in markets:
            try:
                market_signals = self._evaluate_market(market)
                signals.extend(market_signals)
            except Exception as exc:
                self.log.debug(
                    "Signal evaluation error for %s: %s",
                    market.market_id[:16],
                    exc,
                )

        if signals:
            self.log.info("Signal strategy produced %d signal(s) this cycle.", len(signals))
        else:
            self.log.debug("Signal strategy: no qualifying opportunities.")

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Per-market evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_market(self, market: MarketInfo) -> List[TradeSignal]:
        """Score a market and return signals if the composite score qualifies."""
        signals = []

        for token in market.tokens:
            score, breakdown = self._score_token(token, market)
            self._update_history(token, market)

            if score >= self.cfg.SIGNAL_MIN_EDGE:
                signal = self._build_signal(token, market, score, breakdown)
                if signal:
                    signals.append(signal)
                    self._log_signal(signal)

        return signals

    def _score_token(
        self, token: TokenInfo, market: MarketInfo
    ) -> tuple[float, dict]:
        """
        Compute a composite score for a single outcome token.

        Returns:
            Tuple of (composite_score, breakdown_dict).
        """
        breakdown = {}

        # ── Signal 1: Volume spike ──────────────────────────────────────────
        vs = self._signal_volume_spike(token.token_id, market.volume)
        breakdown["volume_spike"] = vs

        # ── Signal 2: Price momentum ────────────────────────────────────────
        mom = self._signal_momentum(token.token_id, token.mid_price)
        breakdown["momentum"] = mom

        # ── Signal 3: Value / mispricing ───────────────────────────────────
        val = self._signal_value(token, market)
        breakdown["value"] = val

        # ── Signal 4: Spread width ─────────────────────────────────────────
        sp = self._signal_spread(token)
        breakdown["spread"] = sp

        composite = (
            WEIGHT_VOLUME_SPIKE * vs
            + WEIGHT_MOMENTUM * mom
            + WEIGHT_VALUE * val
            + WEIGHT_SPREAD * sp
        )

        self.log.debug(
            "Score %.3f for %s/%s | vs=%.2f mom=%.2f val=%.2f sp=%.2f",
            composite,
            market.question[:40],
            token.outcome,
            vs,
            mom,
            val,
            sp,
        )

        return composite, breakdown

    # ─────────────────────────────────────────────────────────────────────────
    # Individual signal functions
    # ─────────────────────────────────────────────────────────────────────────

    def _signal_volume_spike(self, token_id: str, current_volume: float) -> float:
        """
        Score 0–1 based on how much current volume exceeds the baseline.

        First observation sets the baseline.  Returns 0 until baseline is set.
        """
        if token_id not in self._volume_baseline:
            if current_volume > 0:
                self._volume_baseline[token_id] = current_volume
            return 0.0

        baseline = self._volume_baseline[token_id]
        if baseline <= 0:
            return 0.0

        ratio = current_volume / baseline
        if ratio >= VOLUME_SPIKE_RATIO:
            # Normalise: ratio of 2× → 0.5, 4× → 1.0
            return min((ratio - 1.0) / (VOLUME_SPIKE_RATIO * 2 - 1), 1.0)
        return 0.0

    def _signal_momentum(self, token_id: str, current_price: float) -> float:
        """
        Score 0–1 based on the magnitude of recent directional price movement.

        Uses the price history buffer (last ~5 minutes of observations).
        """
        history = self._price_history.get(token_id, [])
        if len(history) < 2 or current_price <= 0:
            return 0.0

        # Use the oldest price in history as the reference
        oldest_price = history[0][1]
        if oldest_price <= 0:
            return 0.0

        change = abs(current_price - oldest_price)
        if change >= MOMENTUM_THRESHOLD:
            return min(change / 0.20, 1.0)  # saturates at 20-cent move
        return 0.0

    def _signal_value(self, token: TokenInfo, market: MarketInfo) -> float:
        """
        Score 0–1 for potential mispricing (underdog value play).

        Targets tokens where the price is low (5–30 cents) and the market
        has meaningful volume — suggesting the market may be underestimating
        the probability of a real outcome.
        """
        price = token.mid_price or token.best_ask
        volume = market.volume

        if not (VALUE_PRICE_LOW <= price <= VALUE_PRICE_HIGH):
            return 0.0

        if volume < VALUE_MIN_VOLUME:
            return 0.0

        # Normalise within the value range:
        # Strongest signal at the midpoint of the range (0.175)
        range_mid = (VALUE_PRICE_HIGH + VALUE_PRICE_LOW) / 2
        distance = abs(price - range_mid)
        range_half = (VALUE_PRICE_HIGH - VALUE_PRICE_LOW) / 2
        position_score = 1.0 - (distance / range_half)

        # Volume boost: high volume in a low-priced market is a stronger signal
        volume_score = min(volume / 100_000.0, 1.0)

        return (position_score * 0.6 + volume_score * 0.4)

    def _signal_spread(self, token: TokenInfo) -> float:
        """
        Score 0–1 for wide bid/ask spread (potential market-making opportunity).

        A wide spread may mean the token is mispriced or illiquid — the
        signal is highest when spread is wide BUT ask is still below 0.50
        (indicating the market thinks this is less likely than not).
        """
        if token.best_ask <= 0 or token.best_bid < 0:
            return 0.0

        spread = token.best_ask - token.best_bid

        if spread < SPREAD_THRESHOLD_WIDE:
            return 0.0

        # Only score if ask price is in a range where upside is realistic
        if not (0.05 <= token.best_ask <= 0.70):
            return 0.0

        # Normalise: 10-cent spread → 0.5, 20-cent → 1.0
        return min(spread / (SPREAD_THRESHOLD_WIDE * 2), 1.0)

    # ─────────────────────────────────────────────────────────────────────────
    # Price history
    # ─────────────────────────────────────────────────────────────────────────

    def _update_history(self, token: TokenInfo, market: MarketInfo) -> None:
        """
        Add current price observation to history.  Keep last 10 observations.
        Also update the volume baseline (rolling average).
        """
        if token.mid_price <= 0:
            return

        history = self._price_history.setdefault(token.token_id, [])
        history.append((time.time(), token.mid_price))

        # Trim to last 10 entries
        if len(history) > 10:
            self._price_history[token.token_id] = history[-10:]

        # Update volume baseline as exponential moving average
        if token.token_id in self._volume_baseline:
            old = self._volume_baseline[token.token_id]
            self._volume_baseline[token.token_id] = 0.9 * old + 0.1 * market.volume
        elif market.volume > 0:
            self._volume_baseline[token.token_id] = market.volume

    # ─────────────────────────────────────────────────────────────────────────
    # Signal construction
    # ─────────────────────────────────────────────────────────────────────────

    def _build_signal(
        self,
        token: TokenInfo,
        market: MarketInfo,
        score: float,
        breakdown: dict,
    ) -> Optional[TradeSignal]:
        """
        Build a GTC limit BUY signal for a qualifying token.

        Places the limit price slightly above the best ask to increase the
        chance of a fill while still capturing most of the spread.
        """
        ask = token.best_ask
        if ask <= 0 or ask >= 0.99:
            return None

        limit_price = min(round(ask + LIMIT_ABOVE_ASK, 4), 0.99)

        # Size: spend up to MAX_POSITION_SIZE, scaled by confidence
        budget = self.cfg.MAX_POSITION_SIZE * score
        size = budget / limit_price if limit_price > 0 else 0
        size = max(round(size, 2), MIN_SHARES)

        reason = (
            f"Signal composite={score:.3f} | "
            f"vs={breakdown.get('volume_spike', 0):.2f} "
            f"mom={breakdown.get('momentum', 0):.2f} "
            f"val={breakdown.get('value', 0):.2f} "
            f"sp={breakdown.get('spread', 0):.2f} | "
            f"{token.outcome} @ {market.question[:40]}"
        )

        return TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=token.token_id,
            side="BUY",
            price=limit_price,
            size=size,
            confidence=score,
            reason=reason,
            order_type="GTC",
        )
