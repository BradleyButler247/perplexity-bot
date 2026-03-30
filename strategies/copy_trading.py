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

from constants import DATA_API, parse_timestamp
from http_client import get_session
from market_scanner import classify_market
from strategies.base import BaseStrategy, TradeSignal

if TYPE_CHECKING:
    from wallet_discovery import WalletDiscovery

logger = logging.getLogger(__name__)


# Maximum price movement (absolute) allowed since target's trade price
MAX_PRICE_DRIFT = 0.05   # 5 cents

# Minimum confidence assigned to copy trades (we trust the target wallet)
BASE_CONFIDENCE = 0.60

# Slightly higher confidence for auto-discovered wallets that met quality gates
DISCOVERED_CONFIDENCE = 0.70

# ── Market cooldown (v41) ─────────────────────────────────────────────────
# Standard wallets wait this long before re-signalling the same market.
# Lowered from 300s (v40) to 120s to catch faster-moving opportunities.
MARKET_COOLDOWN_STANDARD = 120   # 2 minutes

# High-confidence wallets (top 3 by score) bypass market cooldown entirely.
HIGH_CONFIDENCE_WALLET_COUNT = 3


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
        self._session = get_session()
        self._wallet_discovery: Optional["WalletDiscovery"] = wallet_discovery

        # Per-wallet seen-trade tracking to support multi-wallet monitoring
        # wallet -> set of trade IDs
        self._wallet_seen: Dict[str, Set[str]] = {}

        # Track which markets we've already signalled this session to
        # prevent duplicate orders on the same market from different wallets
        # or repeated API responses.  market_id -> timestamp of last signal
        self._market_signalled: Dict[str, float] = {}

        # Track which token_id we bet on per market to prevent betting both sides.
        # market_id -> token_id we already bought
        self._market_side_taken: Dict[str, str] = {}

        # ── v41: Wallet tier cache ────────────────────────────────────────
        # Set of wallet addresses classified as "high confidence" (top N by
        # composite score).  Refreshed each scan cycle from WalletDiscovery.
        self._high_confidence_wallets: Set[str] = set()

        # ── v41: Profitable exit tracking ─────────────────────────────────
        # Markets where we previously held a copy-trade position that closed
        # profitably.  These markets are eligible for re-entry even if they
        # appear in _market_signalled or _market_side_taken.
        # market_id -> True
        self._profitable_exits: Dict[str, bool] = {}

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

        # ── v41: Refresh wallet tiers + profitable exits ──────────────────
        self._refresh_wallet_tiers()
        self._refresh_profitable_exits()

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
        trade_ts = parse_timestamp(trade)
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

        # ── v41: Check if this is a profitable-exit re-entry ────────────
        is_reentry = market_id in self._profitable_exits

        # ── Conflict prevention ────────────────────────────────────────
        # 1. Market cooldown (v41: tier-aware + re-entry bypass)
        #    High-confidence wallets bypass cooldown entirely.
        #    Profitable-exit markets are also exempt (re-entry allowed).
        is_high_conf_wallet = wallet in self._high_confidence_wallets
        last_signal_time = self._market_signalled.get(market_id, 0)
        cooldown_elapsed = time.time() - last_signal_time

        if not is_high_conf_wallet and not is_reentry:
            if cooldown_elapsed < MARKET_COOLDOWN_STANDARD:
                self.log.debug(
                    "Market %s already signalled %.0fs ago (cooldown=%ds, tier=standard); skipping.",
                    market_id[:16],
                    cooldown_elapsed,
                    MARKET_COOLDOWN_STANDARD,
                )
                return None
        elif is_high_conf_wallet and last_signal_time > 0:
            self.log.debug(
                "Market %s: high-confidence wallet %s bypasses cooldown (%.0fs ago).",
                market_id[:16], wallet[:10], cooldown_elapsed,
            )
        elif is_reentry:
            self.log.info(
                "Market %s: re-entry allowed (previous position closed profitably).",
                market_id[:16],
            )
            # Clear the profitable-exit flag so we don't re-enter endlessly
            self._profitable_exits.pop(market_id, None)
            # Also clear the stale side-taken record for this market
            self._market_side_taken.pop(market_id, None)

        # 2. Both-sides prevention: if we already bet on a different token
        #    in this market (e.g., wallet A says Yes, wallet B says No),
        #    don't take the opposite side — that cancels out our position.
        existing_token = self._market_side_taken.get(market_id)
        if existing_token and existing_token != token_id:
            self.log.info(
                "Both-sides blocked: market %s already has token %s, "
                "rejecting opposite token %s from wallet %s.",
                market_id[:16], existing_token[:16],
                token_id[:16], wallet[:10],
            )
            return None

        # 3. Check existing open positions — don't enter a market where
        #    we already hold a position (prevents stacking on same market)
        if hasattr(self, 'market_scanner') and self.market_scanner:
            try:
                from position_tracker import PositionTracker
                tracker = getattr(self, '_position_tracker', None)
                if tracker is None:
                    # Try to get it from the risk manager
                    tracker = getattr(self.risk_manager, 'tracker', None)
                if tracker:
                    for pos in tracker.get_all_positions():
                        if pos.market_id == market_id:
                            self.log.debug(
                                "Already hold position in market %s; skipping duplicate.",
                                market_id[:16],
                            )
                            return None
            except Exception:
                pass  # Position tracker not accessible, skip check

        # Trade price at which target bought
        target_price = float(trade.get("price") or trade.get("avgPrice") or 0)
        if target_price <= 0 or target_price >= 1.0:
            return None

        # ── Price cap: skip entries above 90¢ ────────────────────────────
        # At 90¢+ the risk/reward is terrible for micro trades.
        # Risking 90¢ to win 10¢ means one loss wipes 9 wins.
        COPY_MAX_PRICE = 0.90
        if target_price > COPY_MAX_PRICE:
            self.log.debug(
                "Trade %s from %s above price cap (%.2f > %.2f); skipping.",
                trade_id, wallet[:10], target_price, COPY_MAX_PRICE,
            )
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

        # ── Category-locking (v34) ───────────────────────────────────────
        # If wallet has category data, check that this market matches
        if self._wallet_discovery is not None:
            market_category = self._get_market_category(market_id, market)
            wallet_categories = self._wallet_discovery.get_wallet_categories(wallet)
            if wallet_categories and market_category:
                if market_category not in wallet_categories:
                    self.log.debug(
                        "Category-lock: wallet %s strong in %s, market is %s; skipping.",
                        wallet[:10], wallet_categories[:3], market_category,
                    )
                    return None

        # Calculate size: COPY_TRADE_SIZE / current_ask gives number of shares
        if current_ask <= 0:
            return None
        size = self.cfg.COPY_TRADE_SIZE / current_ask

        # ── Dynamic confidence scoring ───────────────────────────────────
        # Instead of flat 0.70, confidence is calculated from:
        #   1. Wallet quality (score, win rate, PnL)
        #   2. Trade quality (entry price sweet spot)
        #   3. Market quality (volume, category match)
        is_manual = bool(self.cfg.TARGET_WALLET)
        if is_manual:
            confidence = BASE_CONFIDENCE
        else:
            confidence = self._calculate_copy_confidence(
                wallet, target_price, market
            )

        # ── v41: Wallet tier label ────────────────────────────────────────
        tier_label = "HIGH" if (not is_manual and wallet in self._high_confidence_wallets) else "STD"
        reentry_label = " RE-ENTRY" if is_reentry else ""

        source_label = (
            f"Manual:{wallet[:10]}…"
            if is_manual
            else f"Discovered:{wallet[:10]}…[{tier_label}]"
        )

        reason = (
            f"Mirror {source_label}{reentry_label} | "
            f"target_price={target_price:.3f} | "
            f"current_ask={current_ask:.3f} | "
            f"drift={price_drift:.4f} | "
            f"conf={confidence:.2f}"
        )

        # Record this market so we don't signal it again within the cooldown
        self._market_signalled[market_id] = time.time()
        # Record which side we took to prevent betting both sides
        self._market_side_taken[market_id] = token_id

        # Add 1¢ above ask to improve fill rate.
        # GTC at exact ask often sits behind other orders and never fills.
        fill_price = round(min(current_ask + 0.01, 0.99), 4)

        return TradeSignal(
            strategy=self.name(),
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            price=fill_price,
            size=round(size, 4),
            confidence=confidence,
            reason=reason,
            order_type="GTC",   # limit order 1¢ above ask for better fills
        )


    def _get_market_category(self, market_id: str, market=None) -> Optional[str]:
        """
        Classify a market into a category using the shared classify_market utility.

        Args:
            market_id: The market condition ID.
            market: Optional MarketInfo if already fetched.

        Returns:
            Category string or None if classification fails.
        """
        if market and hasattr(market, "question"):
            return classify_market(market.question)

        # Try to get the market from scanner
        if self.market_scanner:
            m = self.market_scanner.get_market(market_id)
            if m:
                return classify_market(m.question)

        return None

    def _calculate_copy_confidence(self, wallet: str, entry_price: float, market) -> float:
        """
        Calculate dynamic confidence for a copy trade based on:

        1. Wallet score (0-1): how well the wallet ranks overall
        2. Wallet win rate: raw historical accuracy
        3. Entry price quality (25-65¢ sweet spot)
        4. Market volume: higher volume = more liquid = safer
        5. Bot bonus: profitable bots are more consistent

        Returns confidence in range [0.30, 0.95].
        """
        confidence = 0.0

        # ── 1. Wallet quality (up to 0.40) ──────────────────────────────
        profile = None
        if self._wallet_discovery:
            profile = self._wallet_discovery.get_wallet_profile(wallet)

        if profile:
            # Wallet composite score (already 0-1)
            confidence += profile.score * 0.30  # Up to 0.30

            # Win rate bonus
            if profile.win_rate >= 0.90:
                confidence += 0.10
            elif profile.win_rate >= 0.70:
                confidence += 0.05
        else:
            # No profile (manual wallet) — default moderate
            confidence += 0.15

        # ── 2. Entry price quality (up to 0.30) ─────────────────────────
        if 0.25 <= entry_price <= 0.65:
            # Sweet spot: best risk/reward
            confidence += 0.30
        elif 0.15 <= entry_price < 0.25 or 0.65 < entry_price <= 0.80:
            # Acceptable range
            confidence += 0.20
        elif entry_price < 0.15:
            # Cheap lottery ticket — lower confidence
            confidence += 0.10
        else:
            # 80-90¢ — poor risk/reward
            confidence += 0.05

        # ── 3. Market volume (up to 0.15) ──────────────────────────────
        if market:
            vol = max(getattr(market, 'volume', 0), getattr(market, 'liquidity', 0))
            if vol >= 1_000_000:
                confidence += 0.15  # Very liquid
            elif vol >= 100_000:
                confidence += 0.10
            elif vol >= 10_000:
                confidence += 0.05

        # ── 4. Bot bonus (up to 0.10) ─────────────────────────────────
        # Profitable bots tend to be more consistent than humans
        if profile and profile.is_likely_bot and profile.pnl > 0:
            confidence += 0.10

        # Clamp to valid range
        confidence = max(0.30, min(0.95, confidence))

        return round(confidence, 2)

    # ─────────────────────────────────────────────────────────────────────────
    # v41: Wallet tiers
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_wallet_tiers(self) -> None:
        """
        Classify discovered wallets into tiers based on composite score.

        Top HIGH_CONFIDENCE_WALLET_COUNT wallets (by WalletDiscovery score)
        are tagged "high confidence" and bypass the per-market cooldown,
        allowing them to signal faster and on markets other wallets already
        touched.

        Only applies to auto-discovered wallets (manual TARGET_WALLET is
        always treated as standard tier since there's only one).
        """
        self._high_confidence_wallets.clear()

        if self.cfg.TARGET_WALLET or self._wallet_discovery is None:
            return

        try:
            profiles = self._wallet_discovery.discover()
        except Exception as exc:
            self.log.debug("Wallet tier refresh failed: %s", exc)
            return

        if not profiles:
            return

        # Profiles are already sorted by score descending from discover()
        top_n = profiles[:HIGH_CONFIDENCE_WALLET_COUNT]
        self._high_confidence_wallets = {p.proxy_wallet for p in top_n}

        self.log.info(
            "Wallet tiers refreshed: %d HIGH (%s), %d STANDARD",
            len(self._high_confidence_wallets),
            [p.proxy_wallet[:10] + "…" for p in top_n],
            len(profiles) - len(self._high_confidence_wallets),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # v41: Profitable exit tracking (re-entry)
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_profitable_exits(self) -> None:
        """
        Scan the position tracker for markets where our copy-trade position
        closed profitably.  These markets become eligible for re-entry.

        A "profitable close" is detected by:
          1. Position is resolved with resolution_price >= 0.99 (we won), OR
          2. Position was sold (SELL in trade history) at a price higher than
             the entry price.

        Only copy_trading positions are considered.
        """
        tracker = self._get_position_tracker()
        if tracker is None:
            return

        # Check resolved positions for wins
        for pos in tracker.get_all_positions(include_resolved=True):
            if not pos.resolved:
                continue
            if pos.market_id in self._profitable_exits:
                continue  # Already tracked
            if pos.market_id in self._market_side_taken:
                # This was a copy-trade market — check if it was profitable
                if pos.resolution_price >= 0.99:
                    # Won: resolution at $1
                    self._profitable_exits[pos.market_id] = True
                    self.log.info(
                        "Profitable exit detected (resolution WIN): market %s",
                        pos.market_id[:16],
                    )
                elif pos.current_price > pos.entry_price:
                    # Sold above entry (trailing stop or manual exit)
                    self._profitable_exits[pos.market_id] = True
                    self.log.info(
                        "Profitable exit detected (sold above entry): market %s "
                        "(entry=%.3f, exit=%.3f)",
                        pos.market_id[:16], pos.entry_price, pos.current_price,
                    )

    def _get_position_tracker(self):
        """
        Retrieve the PositionTracker from the risk manager.

        Returns:
            PositionTracker instance or None if not accessible.
        """
        tracker = getattr(self, '_position_tracker', None)
        if tracker is not None:
            return tracker
        if self.risk_manager:
            tracker = getattr(self.risk_manager, 'tracker', None)
            if tracker is not None:
                return tracker
        return None

