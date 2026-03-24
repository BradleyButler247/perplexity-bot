"""
market_scanner.py
-----------------
Discovers and filters active Polymarket markets from the Gamma API, then
enriches them with live pricing and order-book data from the CLOB API.

Key behaviours:
  • Fetches all active, order-book-enabled markets from Gamma.
  • Applies configurable liquidity / volume filters.
  • Caches market data with a configurable TTL to avoid hammering rate limits.
  • Exposes per-token mid-price, best bid/ask, and order-book depth.

Rate limits (public endpoints): ~100 requests/minute.
"""

import datetime
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
from py_clob_client.client import ClobClient

from config import Config

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
GAMMA_MARKETS_URL = f"{GAMMA_API}/markets"


@dataclass
class TokenInfo:
    """Represents one outcome token (Yes or No) within a market."""

    token_id: str
    outcome: str           # "Yes" or "No"
    mid_price: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 1.0
    bid_size: float = 0.0
    ask_size: float = 0.0


@dataclass
class MarketInfo:
    """Enriched market snapshot combining Gamma metadata and CLOB prices."""

    market_id: str          # Polymarket condition ID (0x…)
    question: str
    volume: float           # Total traded volume in USDC
    liquidity: float        # Current liquidity in USDC
    end_date: str
    tokens: List[TokenInfo] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)

    @property
    def yes_token(self) -> Optional[TokenInfo]:
        for t in self.tokens:
            if t.outcome.lower() == "yes":
                return t
        return self.tokens[0] if self.tokens else None

    @property
    def no_token(self) -> Optional[TokenInfo]:
        for t in self.tokens:
            if t.outcome.lower() == "no":
                return t
        return self.tokens[1] if len(self.tokens) > 1 else None


class MarketScanner:
    """
    Discovers, filters, and prices Polymarket markets.

    Usage:
        scanner = MarketScanner(config, clob_client)
        markets = scanner.get_markets()
    """

    # Time (seconds) before a cached snapshot is considered stale
    MARKET_TTL = 120
    # Maximum markets to enrich per scan cycle (keeps API calls bounded)
    MAX_MARKETS = 100

    def __init__(self, cfg: Config, client: ClobClient) -> None:
        self.cfg = cfg
        self.client = client
        self._cache: Dict[str, MarketInfo] = {}  # market_id -> MarketInfo
        self._last_full_scan: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def get_markets(self, force_refresh: bool = False) -> List[MarketInfo]:
        """
        Return a list of enriched, tradeable markets.

        Results are cached for MARKET_TTL seconds.  Pass force_refresh=True
        to bypass the cache and fetch fresh data immediately.

        Returns:
            List of MarketInfo objects sorted by volume descending.
        """
        now = time.time()
        if force_refresh or (now - self._last_full_scan) > self.MARKET_TTL:
            self._refresh_markets()
            self._last_full_scan = now

        return sorted(self._cache.values(), key=lambda m: m.volume, reverse=True)

    def get_market(self, market_id: str) -> Optional[MarketInfo]:
        """Return a single cached MarketInfo by condition ID, or None."""
        return self._cache.get(market_id)

    def refresh_prices(self, market_ids: Optional[List[str]] = None) -> None:
        """
        Refresh CLOB prices for specific markets (or all cached markets).

        Call this between scan cycles to keep prices fresh without doing a
        full Gamma API refresh.
        """
        targets = market_ids or list(self._cache.keys())
        for mid in targets:
            market = self._cache.get(mid)
            if market:
                self._enrich_market(market)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _ts() -> str:
        """Compact timestamp for console progress."""
        return datetime.datetime.now().strftime("%H:%M:%S")

    def _refresh_markets(self) -> None:
        """Fetch active markets from Gamma API and enrich with CLOB data."""
        logger.info("Scanning Gamma API for active markets…")
        print(f"  [{self._ts()}] 📡 Fetching market list from Gamma API...", flush=True)
        raw_markets = self._fetch_gamma_markets()
        logger.info("Gamma returned %d raw markets.", len(raw_markets))
        print(f"  [{self._ts()}] 📡 Gamma returned {len(raw_markets)} markets", flush=True)

        # Apply basic filters
        filtered = self._filter_markets(raw_markets)
        logger.info(
            "%d markets pass liquidity/volume filters (min_liquidity=%s).",
            len(filtered),
            self.cfg.MIN_LIQUIDITY,
        )

        # Limit to top N by volume to keep API calls bounded
        top = sorted(filtered, key=lambda m: float(m.get("volume", 0)), reverse=True)[
            : self.MAX_MARKETS
        ]
        print(
            f"  [{self._ts()}] 📡 Enriching top {len(top)} markets with live prices...",
            flush=True,
        )

        # Enrich with live pricing (show progress)
        enriched_count = 0
        total = len(top)
        for i, raw in enumerate(top, 1):
            try:
                market = self._build_market_info(raw)
                if market:
                    self._enrich_market(market)
                    self._cache[market.market_id] = market
                    enriched_count += 1
            except Exception as exc:
                logger.debug(
                    "Skipping market %s during enrichment: %s",
                    raw.get("conditionId", "?"),
                    exc,
                )

            # Progress indicator every 10 markets or on the last one
            if i % 10 == 0 or i == total:
                pct = int(i / total * 100)
                bar_filled = pct // 5
                bar = "█" * bar_filled + "░" * (20 - bar_filled)
                sys.stdout.write(
                    f"\r  [{self._ts()}] 📡 Enriching: {bar} {i}/{total} ({pct}%)   "
                )
                sys.stdout.flush()

        # Clear progress line and print final status
        sys.stdout.write("\r" + " " * 70 + "\r")
        sys.stdout.flush()
        print(
            f"  [{self._ts()}] ✅ Market scan complete: {enriched_count} markets ready",
            flush=True,
        )
        logger.info("Market scan complete. %d markets ready.", enriched_count)

    def _fetch_gamma_markets(self) -> List[dict]:
        """
        Fetch all active, order-book-enabled markets from the Gamma API.

        The Gamma API supports cursor-based pagination; we iterate until all
        pages are consumed.
        """
        markets: List[dict] = []

        # Fetch high-volume markets first by sorting server-side.
        # Use a large page size and cap total pages to avoid pulling
        # tens of thousands of low-activity markets we'll never trade.
        params = {
            "active": "true",
            "enableOrderBook": "true",
            "closed": "false",
            "limit": 100,
            "offset": 0,
            "order": "volume",
            "ascending": "false",
        }

        # We only enrich top MAX_MARKETS (100), so fetching 500 total
        # gives plenty of margin after filtering.
        max_markets_to_fetch = 500
        max_pages = max_markets_to_fetch // params["limit"]

        page_num = 0
        while page_num < max_pages:
            page_num += 1
            try:
                sys.stdout.write(
                    f"\r  [{self._ts()}] 📡 Fetching page {page_num}/{max_pages} "
                    f"({len(markets)} markets so far)...   "
                )
                sys.stdout.flush()

                resp = self._session.get(
                    GAMMA_MARKETS_URL, params=params, timeout=20
                )
                resp.raise_for_status()
                page = resp.json()
            except requests.RequestException as exc:
                logger.error("Gamma API request failed: %s", exc)
                print(f"\n  [{self._ts()}] ⚠️  Gamma API error: {exc}", flush=True)
                break

            if isinstance(page, list):
                markets.extend(page)
                if len(page) < params["limit"]:
                    break  # No more pages
                params["offset"] += params["limit"]  # type: ignore[operator]
            else:
                # Some API versions return a dict with a 'data' key
                data = page.get("data") or page.get("markets") or []
                markets.extend(data)
                break

        # Clear the progress line
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()

        return markets

    def _filter_markets(self, markets: List[dict]) -> List[dict]:
        """
        Apply liquidity and activity filters.

        Keeps markets that:
          - Have at least one token ID (required to trade)
          - Have volume >= MIN_LIQUIDITY
        """
        filtered = []
        for m in markets:
            tokens = m.get("clobTokenIds") or m.get("tokens") or []
            if not tokens:
                continue
            volume = float(m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            if max(volume, liquidity) < self.cfg.MIN_LIQUIDITY:
                continue
            filtered.append(m)
        return filtered

    def _build_market_info(self, raw: dict) -> Optional[MarketInfo]:
        """
        Convert a raw Gamma API market dict into a MarketInfo object.

        The Gamma API uses two slightly different shapes depending on the
        endpoint version; this method handles both.
        """
        market_id = raw.get("conditionId") or raw.get("id")
        if not market_id:
            return None

        # Token IDs: Gamma returns them as a JSON-encoded string list or plain list
        token_ids_raw = raw.get("clobTokenIds") or raw.get("tokens") or []
        if isinstance(token_ids_raw, str):
            import json
            try:
                token_ids_raw = json.loads(token_ids_raw)
            except Exception:
                token_ids_raw = []

        outcomes = raw.get("outcomes") or ["Yes", "No"]
        if isinstance(outcomes, str):
            import json
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = ["Yes", "No"]

        tokens = []
        for i, tid in enumerate(token_ids_raw):
            outcome_label = outcomes[i] if i < len(outcomes) else f"Token{i}"
            tokens.append(TokenInfo(token_id=str(tid), outcome=outcome_label))

        return MarketInfo(
            market_id=market_id,
            question=raw.get("question", "Unknown"),
            volume=float(raw.get("volume", 0) or 0),
            liquidity=float(raw.get("liquidity", 0) or 0),
            end_date=raw.get("endDate") or raw.get("end_date", ""),
            tokens=tokens,
        )

    def _enrich_market(self, market: MarketInfo) -> None:
        """
        Fetch live mid-price and order-book data from the CLOB for each token.

        Failures are logged at DEBUG level and the token is left with default
        prices (0.0 / 1.0) to avoid crashing the scan.
        """
        for token in market.tokens:
            try:
                # Mid-price
                mid_resp = self.client.get_midpoint(token.token_id)
                if mid_resp:
                    token.mid_price = float(mid_resp.get("mid", 0))
            except Exception as exc:
                logger.debug("mid-price fetch failed for %s: %s", token.token_id, exc)

            try:
                # Order book top-of-book
                book = self.client.get_order_book(token.token_id)
                if book:
                    bids = book.bids or []
                    asks = book.asks or []
                    if bids:
                        # bids sorted descending; first = best bid
                        best = bids[0]
                        token.best_bid = float(best.get("price", 0) if isinstance(best, dict) else best[0])
                        token.bid_size = float(best.get("size", 0) if isinstance(best, dict) else best[1])
                    if asks:
                        # asks sorted ascending; first = best ask
                        best = asks[0]
                        token.best_ask = float(best.get("price", 1) if isinstance(best, dict) else best[0])
                        token.ask_size = float(best.get("size", 0) if isinstance(best, dict) else best[1])
            except Exception as exc:
                logger.debug("order-book fetch failed for %s: %s", token.token_id, exc)

        market.fetched_at = time.time()
        logger.debug(
            "Enriched: %s | YES ask=%.3f | NO ask=%.3f",
            market.question[:60],
            market.yes_token.best_ask if market.yes_token else 0,
            market.no_token.best_ask if market.no_token else 0,
        )

    def get_liquidity_depth(
        self, token_id: str, side: str = "BUY", max_usd: float = 100.0
    ) -> Tuple[float, float]:
        """
        Calculate available liquidity depth up to max_usd.

        Returns:
            Tuple of (total_size_available, weighted_average_price).
        """
        try:
            book = self.client.get_order_book(token_id)
            if not book:
                return 0.0, 0.0

            levels = book.asks if side.upper() == "BUY" else book.bids
            if not levels:
                return 0.0, 0.0

            total_size = 0.0
            total_cost = 0.0
            remaining_usd = max_usd

            for level in levels:
                if isinstance(level, dict):
                    price = float(level["price"])
                    size = float(level["size"])
                else:
                    price, size = float(level[0]), float(level[1])

                level_cost = price * size
                if level_cost <= remaining_usd:
                    total_size += size
                    total_cost += level_cost
                    remaining_usd -= level_cost
                else:
                    # Partial fill of this level
                    fill_size = remaining_usd / price
                    total_size += fill_size
                    total_cost += remaining_usd
                    break

            avg_price = total_cost / total_size if total_size > 0 else 0.0
            return total_size, avg_price

        except Exception as exc:
            logger.debug("get_liquidity_depth failed for %s: %s", token_id, exc)
            return 0.0, 0.0
