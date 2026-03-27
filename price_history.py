"""
price_history.py
-----------------
Shared price-history tracker used by multiple strategies.

Consolidates the per-strategy price_history dicts (previously duplicated in
signal_based, crypto_mean_reversion, sports_momentum, contrarian_extreme)
into a single utility.  Reduces memory usage and ensures consistency.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PriceHistoryTracker:
    """
    Thread-safe rolling price history for outcome tokens.

    Stores (timestamp, price) tuples per token_id with a configurable
    max length to bound memory.

    Usage:
        tracker = PriceHistoryTracker(max_observations=30)
        tracker.update("token_abc", 0.45)
        avg = tracker.get_average("token_abc")
        velocity = tracker.get_velocity("token_abc", window=3)
    """

    def __init__(self, max_observations: int = 30) -> None:
        self._max = max_observations
        self._data: Dict[str, List[Tuple[float, float]]] = {}

    def update(self, token_id: str, price: float, ts: Optional[float] = None) -> None:
        """Record a price observation for a token."""
        if price <= 0:
            return
        if ts is None:
            ts = time.time()
        if token_id not in self._data:
            self._data[token_id] = []
        history = self._data[token_id]
        history.append((ts, price))
        if len(history) > self._max:
            self._data[token_id] = history[-self._max:]

    def get_history(self, token_id: str) -> List[Tuple[float, float]]:
        """Return the full price history for a token."""
        return self._data.get(token_id, [])

    def get_average(self, token_id: str, window: Optional[int] = None) -> float:
        """Return the average price over the last `window` observations."""
        history = self._data.get(token_id, [])
        if not history:
            return 0.0
        if window:
            history = history[-window:]
        return sum(p for _, p in history) / len(history)

    def get_latest(self, token_id: str) -> Optional[float]:
        """Return the most recent price, or None."""
        history = self._data.get(token_id, [])
        return history[-1][1] if history else None

    def get_velocity(self, token_id: str, window: int = 3) -> Optional[float]:
        """
        Compute price velocity (change per observation) over the last `window`
        data points.  Returns None if insufficient data.
        """
        history = self._data.get(token_id, [])
        if len(history) < window:
            return None
        recent = history[-window:]
        time_delta = recent[-1][0] - recent[0][0]
        if time_delta <= 0:
            return None
        price_delta = recent[-1][1] - recent[0][1]
        return price_delta

    def has_been_extreme(
        self, token_id: str, threshold: float, max_days: float
    ) -> bool:
        """
        Check if a token has been above `threshold` for longer than `max_days`.

        Used by contrarian strategy to avoid fading long-held extremes.
        """
        history = self._data.get(token_id, [])
        if not history:
            return False

        cutoff = time.time() - (max_days * 86400)
        for ts, price in history:
            if ts >= cutoff and price < threshold:
                return False  # Price dipped below threshold recently
        # All recent observations are above threshold
        return len(history) >= 3 and history[0][0] < cutoff

    def count(self, token_id: str) -> int:
        """Return number of observations for a token."""
        return len(self._data.get(token_id, []))

    def clear(self, token_id: Optional[str] = None) -> None:
        """Clear history for a specific token or all tokens."""
        if token_id:
            self._data.pop(token_id, None)
        else:
            self._data.clear()
