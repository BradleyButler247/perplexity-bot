"""
strategies/copy_trading.py
--------------------------
Copy-trading strategy: mirror BUY activity from target wallets.

Two modes of operation:
  1. Manual target wallet (TARGET_WALLET is set in config):
     Mirrors a single configured wallet, ignoring auto-discovery.

  2. Auto-discovery (AUTO_DISCOVER_WALLETS=true, TARGET_WALLET not set):
     Uses WalletDiscovery to find the top-performing wallets on the
     Polymarket leaderboard, then mirrors up to MAX_COPY_WALLETS of them
     simultaneously.

Safeguards (apply in both modes):
  • Only mirrors BUY trades (not SELL) — avoids chasing exits.
  • Skips trades older than COPY_TRADE_MAX_AGE seconds.
  • Skips if the market has moved significantly since the target traded.
  • De-duplicates by tracking already-mirrored trade IDs.
  • Skips markets with insufficient liquidity.

Data API endpoint (public, no auth):
  GET https://data-api.polymarket.com/activity?user={wallet}&type=TRADE&limit=50
"""

import logging
import time
from typing import Dict, List, Optional, Set, TYPE_CHECKING

import requests

from strategies.base import BaseStrategy, TradeSignal

if TYPE_CHECKING:
    from wallet_discovery import WalletDiscovery

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"

# Maximum price movement (absolute) allowed since target's trade price
MAX_PRICE_DRIFT = 0.05   # 5 cents

# Minimum confidence assigned to copy trades (we trust the target wallet)
BASE_CONFIDENCE = 0.60

# Slightly higher confidence for auto-discovered wallets that met quality gates
DISCOVERED_CONFIDENCE = 0.70


class CopyTradingStrategy(BaseStrategy):
    """
    Mirrors BUY positions placed by one or more target wallets.

    Maintains an internal set of trade IDs that have already been processed
    to avoid duplicating signals across scan cycles.

    When wallet auto-discovery is enabled and no TARGET_WALLET is configured,
    the strategy fetches discovered wallets from WalletDiscovery on each scan
    cycle (discovery results are internally cached by WalletDiscovery).
    """

    def name(self) -> str:
        return "copy_trading"

    def __init__(self, *args, wallet_discovery: Optional["WalletDiscovery"] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._seen_trade_ids: Set[str] = set()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._wallet_discovery: Optional["WalletDiscovery"] = wallet_discovery

        # Per-wallet seen-trade tracking to support multi-wallet monitoring
        # wallet -> set of trade IDs
        self._wallet_seen: Dict[str, Set[str]] = {}

        # Track which markets we've already signalled this session to
        # prevent duplicate orders on the same market from different wallets
        # or repeated API responses.  market_id -> timestamp of last signal
        self._market_signalled: Dict[str, float] = {}

    def scan(self) -> List[TradeSignal]:
        """
        Fetch recent target wallet activity and generate mirror signals.

        Returns:
            List of TradeSignal objects for new qualifying BUY trades.
        """
        wallets = self._get_target_wallets()

        if not wallets:
            self.log.debug(
                "No target wallets configured and auto-discovery disabled or yielded no results. "
                "Skipping copy-trading scan."
            )
            return []

        signals: List[TradeSignal] = []

        for wallet in wallets:
            wallet_signals = self._scan_wallet(wallet)
            signals.extend(wallet_signals)

        if signals:
            self.log.info(
                "Copy-trading signals this cycle: %d (across %d wallet(s))",
                len(signals),
                len(wallets),
            )

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # Wallet resolution
    # ─────────────────────────────────────────────────────────────────────────

    def _get_target_wallets(self) -> List[str]:
        """
        Resolve the list of wallets to monitor this cycle.

        Priority:
          1. If TARGET_WALLET is set (manual override) → use only that wallet.
          2. If AUTO_DISCOVER_WALLETS is True and WalletDiscovery is available
             → return discovered wallet addresses.
          3. Otherwise → return empty list (no scanning).

        Returns:
            List of lowercase wallet address strings.
        """
        if self.cfg.TARGET_WALLET:
            # Manual override always takes precedence
            return [self.cfg.TARGET_WALLET.lower()]

        if self.cfg.AUTO_DISCOVER_WALLETS and self._wallet_discovery is not None:
            try:
                wallets = self._wallet_discovery.get_wallet_addresses()
                if wallets:
                    self.log.debug(
                        "Auto-discovery provided %d wallet(s): %s",
                        len(wallets),
                        [w[:10] + "…" for w in wallets],
                    )
                    return wallets
                else:
                    self.log.debug("Auto-discovery returned no wallets this cycle.")
            except Exception as exc:
                self.log.warning("WalletDiscovery failed: %s", exc)

        return []

    # ─────────────────────────────────────────────────────────────────────────
    # Per-wallet scanning
    # ─────────────────────────────────────────────────────────────────────────

    def _scan_wallet(self, wallet: str) -> List[TradeSignal]:
        """
        Scan a single wallet's recent activity for copy-trade opportunities.

        Args:
            wallet: The proxy wallet address to monitor.

        Returns:
            List of new TradeSignal objects.
        """
        if wallet not in self._wallet_seen:
            self._wallet_seen[wallet] = set()

        trades = self._fetch_trades(wallet)
        if not trades:
            return []

        signals: List[TradeSignal] = []

        for trade in trades:
            try:
                signal = self._evaluate_trade(trade, wallet)
                if signal:
                    signals.append(signal)
                    self._log_signal(signal)
            except Exception as exc:
                self.log.debug(
                    "Error evaluating trade %s from wallet %s: %s",
                    trade.get("id"),
                    wallet[:10],
                    exc,
                )

        return signals

    def _fetch_trades(self, wallet: str) -> List[dict]:
        """
        Fetch recent TRADE activity for a wallet from the Data API.

        Args:
            wallet: The proxy wallet address.

        Returns:
            List of raw trade dicts (newest first).
        """
        url = f"{DATA_API}/activity"
        params = {
            "user": wallet,
            "type": "TRADE",
            "limit": 50,
        }
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json() or []
        except requests.RequestException as exc:
            self.log.warning("Data API request failed for wallet %s: %s", wallet[:10], exc)
            return []

    def _evaluate_trade(self, trade: dict, wallet: str) -> Optional[TradeSignal]:
        """
        Decide whether to mirror a single trade from a target wallet.

        Args:
            trade:  Raw trade dict from the Data API.
            wallet: The wallet address this trade belongs to.

        Returns:
            A TradeSignal if the trade should be mirrored, or None.
        """
        seen = self._wallet_seen[wallet]

        # Build a unique trade ID: prefer explicit ID fields, fall back to
        # a composite of (wallet, timestamp, conditionId) to avoid re-processing
        trade_id = str(
            trade.get("id")
            or trade.get("tradeId")
            or trade.get("transactionHash")
            or ""
        )
        if not trade_id:
            # Synthetic ID from available fields to ensure deduplication
            ts = trade.get("timestamp") or trade.get("createdAt") or ""
            cid = trade.get("conditionId") or trade.get("market") or ""
            trade_id = f"{wallet}_{ts}_{cid}"

        # Skip already-processed trades
        if trade_id in seen:
            return None

        # Mark as seen regardless of outcome to avoid re-processing
        seen.add(trade_id)
        # Also track globally to prevent double-signalling if the same trade
        # appears across multiple wallets (rare but possible)
        self._seen_trade_ids.add(trade_id)

        # Only mirror BUY side
        side = (trade.get("side") or trade.get("type") or "").upper()
        if side not in ("BUY", "LONG"):
            return None

        # Age check: skip if the trade is stale
        trade_ts = _parse_timestamp(trade)
        if trade_ts and (time.time() - trade_ts) > self.cfg.COPY_TRADE_MAX_AGE:
            self.log.debug(
                "Trade %s from wallet %s too old (%.0fs ago); skipping.",
                trade_id,
                wallet[:10],
                time.time() - trade_ts,
            )
            return None

        # Extract token and market IDs
        token_id = str(
            trade.get("asset")
            or trade.get("assetId")
            or trade.get("asset_id")
            or trade.get("tokenId")
            or ""
        )
        market_id = str(
            trade.get("conditionId")
            or trade.get("market")
            or trade.get("marketId")
            or ""
        )
        if not token_id or not market_id:
            self.log.debug("Trade %s missing token/market IDs.", trade_id)
            return None

        # Prevent duplicate signals on the same market within a 5-minute window.
        # This catches cases where multiple wallets trade the same market,
        # or the API returns the same trade with different IDs.
        last_signal_time = self._market_signalled.get(market_id, 0)
        if time.time() - last_signal_time < 300:  # 5-minute cooldown per market
            self.log.debug(
                "Market %s already signalled %.0fs ago; skipping duplicate.",
                market_id[:16],
                time.time() - last_signal_time,
            )
            return None

        # Trade price at which target bought
        target_price = float(trade.get("price") or trade.get("avgPrice") or 0)
        if target_price <= 0 or target_price >= 1.0:
            return None

        # Current market price: check it hasn't drifted too far
        market = self.market_scanner.get_market(market_id)
        current_ask: float = target_price  # fallback if market not cached

        if market:
            for token in market.tokens:
                if token.token_id == token_id:
                    current_ask = token.best_ask
                    break
            # Liquidity gate
            if max(market.volume, market.liquidity) < self.cfg.MIN_LIQUIDITY:
                self.log.debug(
                    "Market %s below liquidity threshold; skipping copy trade from %s.",
                    market_id[:16],
                    wallet[:10],
                )
                return None

        price_drift = abs(current_ask - target_price)
        if price_drift > MAX_PRICE_DRIFT:
            self.log.info(
                "Price drifted %.4f since wallet %s trade (target=%.3f, now=%.3f); skipping.",
                price_drift,
                wallet[:10],
                target_price,
                current_ask,
            )
            return None

        # Calculate size: COPY_TRADE_SIZE / current_ask gives number of shares
        if current_ask <= 0:
            return None
        size = self.cfg.COPY_TRADE_SIZE / current_ask

        # Determine confidence and source label
        is_manual = bool(self.cfg.TARGET_WALLET)
        confidence = BASE_CONFIDENCE if is_manual else DISCOVERED_CONFIDENCE
        source_label = (
            f"Manual:{wallet[:10]}…"
            if is_manual
            else f"Discovered:{wallet[:10]}…"
        )

        reason = (
            f"Mirror {source_label} | "
            f"target_price={target_price:.3f} | "
            f"current_ask={current_ask:.3f} | "
            f"drift={price_drift:.4f}"
        )

        # Record this market so we don't signal it again within the cooldown
        self._market_signalled[market_id] = time.time()

        return TradeSignal(
            strategy=self.name(),
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            price=round(current_ask, 4),
            size=round(size, 4),
            confidence=confidence,
            reason=reason,
            order_type="GTC",   # use limit order at current ask
        )


def _parse_timestamp(trade: dict) -> Optional[float]:
    """
    Extract a Unix timestamp (seconds) from a trade dict.

    Handles multiple common field names and both seconds and milliseconds.
    """
    for key in ("timestamp", "createdAt", "created_at", "time", "ts"):
        val = trade.get(key)
        if val:
            try:
                ts = float(val)
                # If the value is in milliseconds (> year 2100 in seconds), convert
                if ts > 4_102_444_800:
                    ts /= 1000.0
                return ts
            except (TypeError, ValueError):
                continue
    return None
