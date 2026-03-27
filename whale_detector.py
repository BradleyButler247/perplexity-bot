"""
whale_detector.py
-----------------
Detects large trades (whale activity) across active Polymarket markets.

Periodically queries the Polymarket Data API for recent large trades
(above WHALE_MIN_TRADE_USD) and tracks markets where sudden large buy
activity has occurred.

Usage:
    detector = WhaleDetector(cfg)
    detector.refresh()  # Call periodically to update spike data
    spikes = detector.get_recent_spikes(minutes=10)
    for spike in spikes:
        print(spike["market_id"], spike["usd_value"])
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import Config
from http_client import get_session

logger = logging.getLogger("bot.whale_detector")

# Polymarket Data API base URL (free, no auth required for reads)
DATA_API_BASE = "https://data-api.polymarket.com"


@dataclass
class WhaleSpike:
    """Represents a single large-trade spike event."""
    market_id: str
    token_id: str
    side: str           # "BUY" or "SELL"
    usd_value: float
    timestamp: float    # Unix timestamp
    trader_address: str


class WhaleDetector:
    """
    Detects whale (large trade) activity across Polymarket markets.

    Exposes get_recent_spikes() so strategies can boost signal confidence
    when a whale has recently traded the same market.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._session = get_session()
        self._spikes: List[WhaleSpike] = []
        self._last_refresh: float = 0.0
        # Minimum seconds between full refreshes (avoid hammering API)
        self._refresh_interval: float = 60.0  # 1 minute

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_recent_spikes(self, minutes: int = 10) -> List[dict]:
        """
        Return all spike events within the last `minutes` minutes.

        Auto-refreshes data if more than _refresh_interval seconds have
        elapsed since the last refresh.

        Args:
            minutes: Lookback window in minutes.

        Returns:
            List of dicts, each with keys:
                market_id, token_id, side, usd_value, timestamp, trader_address
        """
        now = time.time()
        if now - self._last_refresh > self._refresh_interval:
            self.refresh()

        cutoff = now - (minutes * 60)
        recent = [
            {
                "market_id": s.market_id,
                "token_id": s.token_id,
                "side": s.side,
                "usd_value": s.usd_value,
                "timestamp": s.timestamp,
                "trader_address": s.trader_address,
            }
            for s in self._spikes
            if s.timestamp >= cutoff
        ]
        return recent

    def is_whale_active(self, market_id: str, minutes: int = 10) -> bool:
        """
        Check if there has been any whale activity in a specific market
        within the last `minutes` minutes.
        """
        spikes = self.get_recent_spikes(minutes=minutes)
        return any(s["market_id"] == market_id for s in spikes)

    def get_market_whale_usd(self, market_id: str, minutes: int = 10) -> float:
        """
        Return the total USD volume of whale trades in a market within
        the lookback window.
        """
        spikes = self.get_recent_spikes(minutes=minutes)
        return sum(s["usd_value"] for s in spikes if s["market_id"] == market_id)

    def refresh(self) -> None:
        """
        Fetch recent large trades from the Polymarket Data API and update
        the spike list.
        """
        try:
            self._fetch_large_trades()
            self._last_refresh = time.time()
            # Prune old spikes (older than 2× the configured lookback)
            cutoff = time.time() - (self.cfg.WHALE_LOOKBACK_MINUTES * 60 * 2)
            self._spikes = [s for s in self._spikes if s.timestamp >= cutoff]
        except Exception as exc:
            logger.debug("WhaleDetector refresh failed: %s", exc)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_large_trades(self) -> None:
        """
        Query the Polymarket Data API for recent large trades.

        Uses the /trades endpoint with a minimum USD size filter.
        """
        min_usd = self.cfg.WHALE_MIN_TRADE_USD
        lookback_s = self.cfg.WHALE_LOOKBACK_MINUTES * 60
        since_ts = int(time.time() - lookback_s)

        try:
            resp = self._session.get(
                f"{DATA_API_BASE}/trades",
                params={
                    "limit": 500,
                    "after": since_ts,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.debug(
                    "WhaleDetector: Data API returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return

            data = resp.json()
            trades = data if isinstance(data, list) else data.get("data", [])

            new_spikes = 0
            seen_ids = {s.token_id + str(s.timestamp) for s in self._spikes}

            for trade in trades:
                try:
                    usd_value = float(trade.get("usdcSize", 0) or trade.get("size", 0))
                    if usd_value < min_usd:
                        continue

                    market_id = str(trade.get("market", "") or trade.get("conditionId", ""))
                    token_id = str(trade.get("asset", "") or trade.get("tokenId", ""))
                    side = str(trade.get("side", "BUY")).upper()
                    timestamp = float(trade.get("timestamp", time.time()))
                    trader = str(trade.get("maker", "") or trade.get("user", "unknown"))

                    dedup_key = token_id + str(timestamp)
                    if dedup_key in seen_ids:
                        continue

                    spike = WhaleSpike(
                        market_id=market_id,
                        token_id=token_id,
                        side=side,
                        usd_value=usd_value,
                        timestamp=timestamp,
                        trader_address=trader,
                    )
                    self._spikes.append(spike)
                    seen_ids.add(dedup_key)
                    new_spikes += 1

                    logger.info(
                        "Whale detected: market=%s side=%s $%.0f trader=%s",
                        market_id[:16], side, usd_value, trader[:16],
                    )

                except (KeyError, ValueError, TypeError) as exc:
                    logger.debug("WhaleDetector: failed to parse trade: %s", exc)

            if new_spikes:
                logger.info(
                    "WhaleDetector: found %d new whale trades (min=$%.0f)",
                    new_spikes, min_usd,
                )

        except Exception as exc:
            logger.debug("WhaleDetector: API fetch error: %s", exc)
