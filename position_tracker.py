"""
position_tracker.py
-------------------
Tracks open positions, calculates P&L, and persists state to disk.

Data sources:
  • Data API (public): GET https://data-api.polymarket.com/positions?user={address}
    — Provides current positions, entry price, size, current value.
  • Local JSON file (positions.json): Persists state across restarts.

Key capabilities:
  • Total portfolio exposure calculation.
  • Per-market and per-token exposure.
  • Unrealised P&L based on current market prices.
  • Detection of resolved markets for realised P&L calculation.
  • Restart recovery from persisted JSON.
"""

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import requests

from config import Config

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
POSITIONS_FILE = "positions.json"


@dataclass
class Position:
    """Represents a single open outcome-token position."""

    token_id: str
    market_id: str
    outcome: str                # "Yes" / "No" / other
    side: str                   # "BUY" or "SELL"
    size: float                 # number of shares
    entry_price: float          # average entry price per share
    current_price: float = 0.0  # latest market price
    opened_at: float = field(default_factory=time.time)
    resolved: bool = False
    resolution_price: float = 0.0  # 0.0 or 1.0 on resolution

    @property
    def cost_basis(self) -> float:
        """Total USDC spent to open this position."""
        return self.entry_price * self.size

    @property
    def current_value(self) -> float:
        """Current mark-to-market value in USDC."""
        if self.resolved:
            return self.resolution_price * self.size
        return self.current_price * self.size

    @property
    def unrealised_pnl(self) -> float:
        """Unrealised P&L in USDC."""
        return self.current_value - self.cost_basis

    @property
    def unrealised_pnl_pct(self) -> float:
        """Unrealised P&L as a percentage of cost basis."""
        if self.cost_basis <= 0:
            return 0.0
        return self.unrealised_pnl / self.cost_basis * 100.0


class PositionTracker:
    """
    Maintains the bot's view of all open positions and P&L.

    Usage:
        tracker = PositionTracker(config, market_scanner)
        tracker.load()
        tracker.refresh()
        print(tracker.total_exposure())
    """

    def __init__(self, cfg: Config, market_scanner=None) -> None:
        self.cfg = cfg
        self.market_scanner = market_scanner
        self._positions: Dict[str, Position] = {}   # token_id -> Position
        self._realised_pnl: float = 0.0
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._wallet_address: Optional[str] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def set_wallet(self, address: str) -> None:
        """Set the wallet address used for Data API position lookups."""
        self._wallet_address = address
        logger.info("Position tracker wallet set: %s", address)

    def load(self) -> None:
        """
        Load persisted positions from the local JSON file.

        Called on startup to restore state after a restart.
        """
        if not os.path.exists(POSITIONS_FILE):
            logger.info("No existing positions file found; starting fresh.")
            return
        try:
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
            for token_id, pos_dict in data.get("positions", {}).items():
                self._positions[token_id] = Position(**pos_dict)
            self._realised_pnl = data.get("realised_pnl", 0.0)
            logger.info(
                "Loaded %d positions from %s | realised_pnl=$%.2f",
                len(self._positions),
                POSITIONS_FILE,
                self._realised_pnl,
            )
        except Exception as exc:
            logger.error("Failed to load positions file: %s", exc)

    def save(self) -> None:
        """Persist current positions to the local JSON file."""
        try:
            data = {
                "positions": {tid: asdict(pos) for tid, pos in self._positions.items()},
                "realised_pnl": self._realised_pnl,
                "saved_at": time.time(),
            }
            with open(POSITIONS_FILE, "w") as f:
                json.dump(data, f, indent=2)
            logger.debug("Positions saved to %s.", POSITIONS_FILE)
        except Exception as exc:
            logger.error("Failed to save positions: %s", exc)

    def refresh(self) -> None:
        """
        Refresh positions from the Data API and update current prices.

        Falls back gracefully if the API is unreachable or the wallet address
        is not configured.
        """
        if self._wallet_address:
            self._fetch_positions_from_api()
        self._update_prices()
        self._check_resolved()
        self.save()

    def record_trade(
        self,
        token_id: str,
        market_id: str,
        outcome: str,
        side: str,
        size: float,
        price: float,
    ) -> None:
        """
        Record a newly executed trade.

        If a position already exists for this token, the entry price is
        updated via weighted average.
        """
        if token_id in self._positions:
            pos = self._positions[token_id]
            # Weighted average entry price
            total_cost = pos.cost_basis + (price * size)
            total_size = pos.size + size
            pos.entry_price = total_cost / total_size if total_size > 0 else price
            pos.size = total_size
            logger.info(
                "Updated position: %s | size=%.2f | avg_entry=%.4f",
                token_id[:16],
                total_size,
                pos.entry_price,
            )
        else:
            self._positions[token_id] = Position(
                token_id=token_id,
                market_id=market_id,
                outcome=outcome,
                side=side,
                size=size,
                entry_price=price,
            )
            logger.info(
                "New position: %s | outcome=%s | size=%.2f @ $%.4f",
                token_id[:16],
                outcome,
                size,
                price,
            )
        self.save()

    def total_exposure(self) -> float:
        """Return total cost basis of all open (non-resolved) positions."""
        return sum(
            p.cost_basis
            for p in self._positions.values()
            if not p.resolved
        )

    def position_count(self) -> int:
        """Return count of open (non-resolved) positions."""
        return sum(1 for p in self._positions.values() if not p.resolved)

    def total_unrealised_pnl(self) -> float:
        """Total unrealised P&L across all open positions."""
        return sum(p.unrealised_pnl for p in self._positions.values() if not p.resolved)

    @property
    def realised_pnl(self) -> float:
        return self._realised_pnl

    def get_all_positions(self) -> List[Position]:
        """Return all tracked positions (open and resolved)."""
        return list(self._positions.values())

    def summary(self) -> str:
        """Return a one-line portfolio summary."""
        return (
            f"Positions: {self.position_count()} open | "
            f"Exposure: ${self.total_exposure():.2f} | "
            f"Unrealised P&L: ${self.total_unrealised_pnl():.2f} | "
            f"Realised P&L: ${self._realised_pnl:.2f}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_positions_from_api(self) -> None:
        """
        Fetch current positions from the Data API and reconcile with local state.

        The Data API returns the canonical on-chain position state; this
        overwrites locally tracked sizes/prices for accuracy.
        """
        url = f"{DATA_API}/positions"
        params = {"user": self._wallet_address}
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            api_positions = resp.json() or []
        except requests.RequestException as exc:
            logger.warning("Data API positions fetch failed: %s", exc)
            return

        for api_pos in api_positions:
            token_id = str(api_pos.get("asset") or api_pos.get("token_id") or "")
            if not token_id:
                continue

            size = float(api_pos.get("size") or api_pos.get("amount") or 0)
            avg_price = float(api_pos.get("avgPrice") or api_pos.get("avg_price") or 0)
            market_id = str(api_pos.get("market") or api_pos.get("condition_id") or "")
            outcome = str(api_pos.get("outcome") or "Unknown")

            if size <= 0:
                # Position fully closed
                if token_id in self._positions and not self._positions[token_id].resolved:
                    pos = self._positions[token_id]
                    realised = pos.current_value - pos.cost_basis
                    self._realised_pnl += realised
                    pos.resolved = True
                    logger.info(
                        "Position closed: %s | realised_pnl=$%.4f",
                        token_id[:16],
                        realised,
                    )
                continue

            cur_price = float(api_pos.get("curPrice") or api_pos.get("cur_price") or 0)
            market_id = str(
                api_pos.get("conditionId")
                or api_pos.get("market")
                or api_pos.get("condition_id")
                or ""
            )

            if token_id in self._positions:
                pos = self._positions[token_id]
                pos.size = size
                if avg_price > 0:
                    pos.entry_price = avg_price
                if cur_price > 0:
                    pos.current_price = cur_price
                if market_id:
                    pos.market_id = market_id
            else:
                self._positions[token_id] = Position(
                    token_id=token_id,
                    market_id=market_id,
                    outcome=outcome,
                    side="BUY",
                    size=size,
                    entry_price=avg_price,
                    current_price=cur_price,
                )

        logger.debug("API position sync complete: %d positions.", len(self._positions))

    def _update_prices(self) -> None:
        """
        Update current_price for all open positions.

        Two-pass approach:
          1. Try the market scanner cache (fast, no API call).
          2. For positions not found in the cache, fetch directly from
             the Data API positions endpoint which includes curPrice.
        """
        missing_price = []

        # Pass 1: use market scanner cache
        for pos in self._positions.values():
            if pos.resolved:
                continue
            found = False
            if self.market_scanner:
                market = self.market_scanner.get_market(pos.market_id)
                if market:
                    for token in market.tokens:
                        if token.token_id == pos.token_id:
                            price = token.mid_price or token.best_bid
                            if price > 0:
                                pos.current_price = price
                                found = True
                            break
            if not found:
                missing_price.append(pos)

        # Pass 2: fetch from Data API for positions not in scanner cache
        if missing_price and self._wallet_address:
            self._refresh_prices_from_api(missing_price)

    def _refresh_prices_from_api(self, positions) -> None:
        """
        Fetch current prices for positions not covered by the market scanner.

        Uses the Data API /positions endpoint which returns curPrice for
        each position held by the wallet.
        """
        try:
            url = f"{DATA_API}/positions"
            params = {
                "user": self._wallet_address,
                "sizeThreshold": 0.01,
                "limit": 200,
            }
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            api_data = resp.json() or []
        except Exception as exc:
            logger.debug("Data API price refresh failed: %s", exc)
            return

        # Build lookup: asset -> curPrice
        price_map = {}
        for entry in api_data:
            asset = entry.get("asset", "")
            cur_price = float(entry.get("curPrice", 0) or 0)
            if asset and cur_price > 0:
                price_map[asset] = cur_price

        # Update positions
        for pos in positions:
            if pos.token_id in price_map:
                pos.current_price = price_map[pos.token_id]
                logger.debug(
                    "Price updated from API: %s -> $%.4f",
                    pos.token_id[:16], pos.current_price,
                )

    def _check_resolved(self) -> None:
        """
        Check if any open position's market has resolved.

        Uses two sources:
          1. Market scanner cache (for tokens in the top 100).
          2. The position's current_price (updated from Data API).

        Positions at price extremes ($1.00 or $0) are marked resolved
        and removed from the active count.
        """
        for pos in list(self._positions.values()):
            if pos.resolved:
                continue

            resolved_price = None

            # Check 1: market scanner cache
            if self.market_scanner:
                market = self.market_scanner.get_market(pos.market_id)
                if market:
                    for token in market.tokens:
                        if token.token_id == pos.token_id:
                            if token.mid_price >= 0.99:
                                resolved_price = 1.0
                            elif 0 < token.mid_price <= 0.01:
                                resolved_price = 0.0
                            break

            # Check 2: current_price from Data API refresh
            if resolved_price is None and pos.current_price >= 0.99:
                resolved_price = 1.0
            elif resolved_price is None and 0 < pos.current_price <= 0.01:
                resolved_price = 0.0

            # Check 3: position size is 0 (closed on-chain)
            if resolved_price is None and pos.size <= 0.01:
                resolved_price = pos.current_price if pos.current_price > 0 else 0.0

            if resolved_price is not None:
                pos.resolved = True
                pos.resolution_price = resolved_price
                if resolved_price >= 0.99:
                    realised = (resolved_price * pos.size) - pos.cost_basis
                    self._realised_pnl += realised
                    logger.info(
                        "Position resolved (WIN): %s | size=%.1f | realised=$%.2f",
                        pos.token_id[:16],
                        pos.size,
                        realised,
                    )
                else:
                    loss = -pos.cost_basis
                    self._realised_pnl += loss
                    logger.info(
                        "Position resolved (LOSS): %s | size=%.1f | realised=$%.2f",
                        pos.token_id[:16],
                        pos.size,
                        loss,
                    )

        # Clean up: remove resolved positions older than 1 hour
        # to keep the tracker lean
        now = time.time()
        to_remove = [
            tid for tid, pos in self._positions.items()
            if pos.resolved and (now - pos.opened_at) > 3600
        ]
        for tid in to_remove:
            del self._positions[tid]
            logger.debug("Removed stale resolved position: %s", tid[:16])
