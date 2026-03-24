"""
wallet_discovery.py
-------------------
Auto-discovers profitable wallets to copy trade from, using the Polymarket
leaderboard and activity APIs.

Discovery flow:
  1. Fetch top traders from the leaderboard across WEEK and MONTH time periods.
  2. For each candidate wallet, fetch their closed positions and recent trades.
  3. Compute a composite score based on win rate, P&L, consistency, and volume.
  4. Return the top N wallets as WalletProfile dataclasses.

Results are cached for WALLET_DISCOVERY_INTERVAL seconds (default 6 hours)
to avoid excessive API calls.

APIs used (all public, no auth):
  GET https://data-api.polymarket.com/v1/leaderboard
  GET https://data-api.polymarket.com/activity?user={wallet}&type=TRADE&limit=100
  GET https://data-api.polymarket.com/closed-positions?user={wallet}
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

from config import Config

logger = logging.getLogger("bot.wallet_discovery")

DATA_API = "https://data-api.polymarket.com"

# Weights for the composite score
SCORE_WEIGHT_WIN_RATE    = 0.40
SCORE_WEIGHT_PNL         = 0.30
SCORE_WEIGHT_CONSISTENCY = 0.20
SCORE_WEIGHT_VOLUME      = 0.10

# Maximum days since last trade to be considered "recently active"
MAX_INACTIVE_DAYS = 7


@dataclass
class WalletProfile:
    """Profile of a discovered, scored trader wallet."""

    proxy_wallet: str
    username: str = ""
    pnl: float = 0.0            # Lifetime/period P&L in USDC
    volume: float = 0.0         # Total traded volume in USDC
    win_rate: float = 0.0       # Fraction of closed positions that were profitable
    closed_positions: int = 0   # Total number of closed positions
    last_trade_ts: float = 0.0  # Unix timestamp of most recent trade
    score: float = 0.0          # Composite score [0.0 – 1.0]
    source_period: str = ""     # "WEEK" / "MONTH" from which this wallet was discovered
    source_category: str = ""   # Leaderboard category

    # Bot-detection metrics
    bot_score: float = 0.0          # 0.0 = human, 1.0 = definitely a bot
    is_likely_bot: bool = False     # True if bot_score >= 0.60
    trades_per_hour: float = 0.0    # Average trade frequency
    avg_inter_trade_sec: float = 0.0  # Avg seconds between consecutive trades
    timing_regularity: float = 0.0  # 0-1, how regular the timing is (1 = clockwork)
    active_hours_ratio: float = 0.0 # Fraction of 24h day with trades (bots ≈ 1.0)
    size_consistency: float = 0.0   # How consistent trade sizes are (1 = identical)

    @property
    def last_trade_days_ago(self) -> float:
        """Days since the wallet's most recent trade."""
        if self.last_trade_ts <= 0:
            return float("inf")
        return (time.time() - self.last_trade_ts) / 86400.0

    def __str__(self) -> str:
        bot_tag = " [BOT]" if self.is_likely_bot else ""
        return (
            f"WalletProfile({self.proxy_wallet[:10]}… | "
            f"user={self.username or 'anon'} | "
            f"score={self.score:.3f} | "
            f"bot={self.bot_score:.2f}{bot_tag} | "
            f"wr={self.win_rate:.1%} | "
            f"pnl=${self.pnl:.0f} | "
            f"closed={self.closed_positions})"
        )


class WalletDiscovery:
    """
    Discovers and ranks profitable Polymarket traders.

    Usage:
        discovery = WalletDiscovery(config)
        wallets = discovery.discover()
        for w in wallets:
            print(w)
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

        # Cache: list of WalletProfile + timestamp of last discovery
        self._cache: List[WalletProfile] = []
        self._cache_ts: float = 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def discover(self, force: bool = False) -> List[WalletProfile]:
        """
        Return the top-scoring wallets, using the cache when fresh.

        Args:
            force: Bypass the cache and re-discover immediately.

        Returns:
            List of WalletProfile sorted by composite score (descending),
            limited to cfg.MAX_COPY_WALLETS.
        """
        now = time.time()
        cache_age = now - self._cache_ts

        if not force and self._cache and cache_age < self.cfg.WALLET_DISCOVERY_INTERVAL:
            logger.debug(
                "WalletDiscovery: returning cached results (age=%.0fs).", cache_age
            )
            return self._cache[: self.cfg.MAX_COPY_WALLETS]

        logger.info("WalletDiscovery: starting fresh discovery run…")
        wallets = self._run_discovery()
        self._cache = wallets
        self._cache_ts = now

        logger.info(
            "WalletDiscovery complete: %d qualified wallets found.", len(wallets)
        )
        for rank, w in enumerate(wallets[: self.cfg.MAX_COPY_WALLETS], start=1):
            logger.info("  #%d: %s", rank, w)

        return wallets[: self.cfg.MAX_COPY_WALLETS]

    def get_wallet_addresses(self, force: bool = False) -> List[str]:
        """
        Convenience method — return only the proxy wallet addresses.

        Returns:
            List of wallet address strings, up to MAX_COPY_WALLETS.
        """
        return [w.proxy_wallet for w in self.discover(force=force)]

    # ─────────────────────────────────────────────────────────────────────────
    # Discovery logic
    # ─────────────────────────────────────────────────────────────────────────

    def _run_discovery(self) -> List[WalletProfile]:
        """
        Execute the full discovery pipeline.

        Returns:
            Scored, sorted list of qualifying WalletProfile objects.
        """
        # 1. Collect candidates from the leaderboard
        candidates: Dict[str, WalletProfile] = {}

        categories = [c.strip().upper() for c in self.cfg.WALLET_CATEGORIES.split(",") if c.strip()]
        periods = ["WEEK", "MONTH"]

        for category in categories:
            for period in periods:
                logger.info(
                    "Fetching leaderboard: category=%s period=%s", category, period
                )
                entries = self._fetch_leaderboard(category=category, time_period=period)
                for entry in entries:
                    wallet = str(entry.get("proxyWallet") or "").lower()
                    if not wallet:
                        continue
                    if wallet not in candidates:
                        candidates[wallet] = WalletProfile(
                            proxy_wallet=wallet,
                            username=str(entry.get("userName") or entry.get("xUsername") or ""),
                            pnl=float(entry.get("pnl") or 0),
                            volume=float(entry.get("vol") or 0),
                            source_period=period,
                            source_category=category,
                        )
                    else:
                        # Keep higher P&L record across time periods
                        existing = candidates[wallet]
                        pnl = float(entry.get("pnl") or 0)
                        if pnl > existing.pnl:
                            existing.pnl = pnl
                            existing.source_period = period

        logger.info(
            "Leaderboard harvest: %d unique candidate wallets.", len(candidates)
        )

        if not candidates:
            return []

        # 2. Enrich each candidate with detailed stats
        qualified: List[WalletProfile] = []

        for wallet_addr, profile in candidates.items():
            try:
                enriched = self._enrich_wallet(profile)
                if enriched is None:
                    continue   # Failed quality filters
                qualified.append(enriched)
            except Exception as exc:
                logger.debug(
                    "Failed to enrich wallet %s: %s", wallet_addr[:10], exc
                )

        # 3. Score and sort
        if not qualified:
            return []

        scored = self._score_wallets(qualified)
        scored.sort(key=lambda w: w.score, reverse=True)
        return scored

    def _enrich_wallet(self, profile: WalletProfile) -> Optional[WalletProfile]:
        """
        Fetch closed positions and recent activity to compute quality metrics.

        Applies quality filters:
          - MIN_CLOSED_POSITIONS: at least N closed positions
          - MIN_WIN_RATE: win rate must meet threshold
          - MAX_INACTIVE_DAYS: must have traded within the last 7 days

        Returns:
            Updated WalletProfile if the wallet qualifies, None if filtered out.
        """
        wallet = profile.proxy_wallet

        # ── Closed positions ────────────────────────────────────────────────
        closed = self._fetch_closed_positions(wallet)
        if len(closed) < self.cfg.MIN_CLOSED_POSITIONS:
            logger.debug(
                "Wallet %s filtered: only %d closed positions (min=%d).",
                wallet[:10],
                len(closed),
                self.cfg.MIN_CLOSED_POSITIONS,
            )
            return None

        profile.closed_positions = len(closed)

        # Compute win rate from closed positions
        win_rate, wins, total = self._compute_win_rate(closed)
        profile.win_rate = win_rate

        if win_rate < self.cfg.MIN_WIN_RATE:
            logger.debug(
                "Wallet %s filtered: win_rate=%.1f%% < %.1f%%.",
                wallet[:10],
                win_rate * 100,
                self.cfg.MIN_WIN_RATE * 100,
            )
            return None

        # ── Recent activity ─────────────────────────────────────────────────
        last_ts = self._fetch_last_trade_timestamp(wallet)
        profile.last_trade_ts = last_ts

        if last_ts > 0:
            days_inactive = (time.time() - last_ts) / 86400.0
            if days_inactive > MAX_INACTIVE_DAYS:
                logger.debug(
                    "Wallet %s filtered: last trade %.1f days ago (max=%d).",
                    wallet[:10],
                    days_inactive,
                    MAX_INACTIVE_DAYS,
                )
                return None

        # ── Bot detection ──────────────────────────────────────────────────
        self._analyze_bot_behavior(profile)

        bot_tag = " [BOT]" if profile.is_likely_bot else ""
        logger.info(
            "Wallet %s qualified | wr=%.1f%% (%d/%d) | closed=%d | pnl=$%.0f | bot=%.2f%s",
            wallet[:10],
            win_rate * 100,
            wins,
            total,
            len(closed),
            profile.pnl,
            profile.bot_score,
            bot_tag,
        )
        return profile

    def _analyze_bot_behavior(self, profile: WalletProfile) -> None:
        """
        Analyze a wallet's trade activity to determine if it's likely a bot.

        Bot indicators (each contributes to bot_score):
          1. High trade frequency (>10 trades/hour)
          2. Regular inter-trade timing (low variance in time between trades)
          3. Round-the-clock activity (trades across many hours of the day)
          4. Consistent position sizing (same amounts repeated)
          5. High total trade count relative to account age

        Updates the profile's bot_score and is_likely_bot fields.
        """
        import statistics

        wallet = profile.proxy_wallet

        # Fetch recent trades (up to 100)
        trades = self._fetch_recent_trades(wallet, limit=100)
        if len(trades) < 10:
            # Not enough data to analyze
            profile.bot_score = 0.0
            return

        # Extract timestamps and sizes
        timestamps = []
        sizes = []
        for t in trades:
            ts = _parse_timestamp(t)
            if ts > 0:
                timestamps.append(ts)
            size = float(t.get("size") or t.get("amount") or t.get("value") or 0)
            if size > 0:
                sizes.append(size)

        timestamps.sort()

        if len(timestamps) < 5:
            profile.bot_score = 0.0
            return

        # ── 1. Trade frequency ──────────────────────────────────────────────
        time_span_hours = (timestamps[-1] - timestamps[0]) / 3600.0
        if time_span_hours > 0:
            trades_per_hour = len(timestamps) / time_span_hours
        else:
            trades_per_hour = 0
        profile.trades_per_hour = trades_per_hour

        # Score: >20/hr = very likely bot, >5/hr = possibly bot
        freq_score = min(trades_per_hour / 20.0, 1.0)

        # ── 2. Timing regularity ────────────────────────────────────────────
        # Calculate inter-trade intervals
        intervals = [
            timestamps[i+1] - timestamps[i]
            for i in range(len(timestamps) - 1)
            if timestamps[i+1] > timestamps[i]
        ]

        if len(intervals) >= 3:
            avg_interval = statistics.mean(intervals)
            profile.avg_inter_trade_sec = avg_interval

            # Coefficient of variation: std_dev / mean
            # Low CV = very regular timing = likely bot
            std_interval = statistics.stdev(intervals)
            cv = std_interval / avg_interval if avg_interval > 0 else 1.0

            # CV < 0.3 = clockwork-like regularity (bot)
            # CV > 1.0 = very irregular (human)
            regularity = max(0, 1.0 - cv)
            profile.timing_regularity = regularity
            timing_score = regularity
        else:
            timing_score = 0.0

        # ── 3. Active hours coverage ────────────────────────────────────────
        # Humans typically trade 8-16 hours/day; bots trade 20-24 hours
        hours_active = set()
        for ts in timestamps:
            import datetime
            dt = datetime.datetime.utcfromtimestamp(ts)
            hours_active.add(dt.hour)

        active_ratio = len(hours_active) / 24.0
        profile.active_hours_ratio = active_ratio

        # Score: >20 unique hours = likely bot
        hours_score = min(active_ratio / 0.85, 1.0)  # 85% of day = 1.0

        # ── 4. Size consistency ─────────────────────────────────────────────
        # Bots tend to use repeating, precise amounts
        if len(sizes) >= 5:
            avg_size = statistics.mean(sizes)
            if avg_size > 0:
                size_cv = statistics.stdev(sizes) / avg_size
                # Low CV = consistent sizing = likely bot
                size_score = max(0, 1.0 - size_cv)
                profile.size_consistency = size_score
            else:
                size_score = 0.0
        else:
            size_score = 0.0

        # ── 5. Volume/frequency ratio ───────────────────────────────────────
        # High trade count + high PnL = sophisticated bot
        volume_freq_score = 0.0
        if profile.closed_positions > 100:
            volume_freq_score = min(profile.closed_positions / 500.0, 1.0)

        # ── Composite bot score ─────────────────────────────────────────────
        profile.bot_score = (
            0.30 * freq_score
            + 0.25 * timing_score
            + 0.20 * hours_score
            + 0.15 * size_score
            + 0.10 * volume_freq_score
        )
        profile.is_likely_bot = profile.bot_score >= 0.60

    def _fetch_recent_trades(self, wallet: str, limit: int = 100) -> List[dict]:
        """
        Fetch recent trade activity for bot-detection analysis.
        """
        url = f"{DATA_API}/activity"
        params = {"user": wallet, "type": "TRADE", "limit": limit}
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except requests.RequestException as exc:
            logger.debug("Trade fetch failed for %s: %s", wallet[:10], exc)
            return []

    def _score_wallets(self, wallets: List[WalletProfile]) -> List[WalletProfile]:
        """
        Assign a composite [0, 1] score to each wallet.

        Scoring formula prioritizes profitable bots:
          score = 0.25 * normalised_win_rate
                + 0.20 * normalised_pnl
                + 0.15 * normalised_consistency    (closed position count)
                + 0.10 * normalised_volume
                + 0.30 * bot_score                 (bots get priority)

        Normalisation is min-max within the candidate set.
        """
        if not wallets:
            return wallets

        def _normalise(values: List[float]) -> List[float]:
            """Min-max normalise a list of floats to [0, 1]."""
            lo, hi = min(values), max(values)
            if hi == lo:
                return [0.5] * len(values)
            return [(v - lo) / (hi - lo) for v in values]

        win_rates   = [w.win_rate for w in wallets]
        pnls        = [w.pnl for w in wallets]
        consistencies = [float(w.closed_positions) for w in wallets]
        volumes     = [w.volume for w in wallets]

        norm_wr  = _normalise(win_rates)
        norm_pnl = _normalise(pnls)
        norm_con = _normalise(consistencies)
        norm_vol = _normalise(volumes)

        for i, wallet in enumerate(wallets):
            wallet.score = (
                0.25 * norm_wr[i]
                + 0.20 * norm_pnl[i]
                + 0.15 * norm_con[i]
                + 0.10 * norm_vol[i]
                + 0.30 * wallet.bot_score
            )

        return wallets

    # ─────────────────────────────────────────────────────────────────────────
    # API helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_leaderboard(
        self,
        category: str = "OVERALL",
        time_period: str = "WEEK",
        limit: int = 50,
    ) -> List[dict]:
        """
        Fetch top traders from the Polymarket leaderboard.

        Args:
            category:    OVERALL, POLITICS, SPORTS, etc.
            time_period: DAY, WEEK, MONTH, ALL.
            limit:       Number of results (1–50).

        Returns:
            List of trader dicts from the API.
        """
        url = f"{DATA_API}/v1/leaderboard"
        params = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": "PNL",
            "limit": limit,
            "offset": 0,
        }
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            # API may return {"data": [...]} or a plain list
            if isinstance(data, list):
                return data
            return data.get("data") or data.get("leaderboard") or []
        except requests.RequestException as exc:
            logger.warning(
                "Leaderboard API failed (category=%s period=%s): %s",
                category,
                time_period,
                exc,
            )
            return []

    def _fetch_closed_positions(self, wallet: str) -> List[dict]:
        """
        Fetch the closed positions for a wallet from the Data API.

        Returns:
            List of position dicts.
        """
        url = f"{DATA_API}/closed-positions"
        params = {"user": wallet}
        try:
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("data") or data.get("positions") or []
        except requests.RequestException as exc:
            logger.debug("Closed positions API failed for %s: %s", wallet[:10], exc)
            return []

    def _fetch_last_trade_timestamp(self, wallet: str) -> float:
        """
        Fetch recent activity and return the timestamp of the most recent trade.

        Returns:
            Unix timestamp in seconds, or 0.0 if unavailable.
        """
        url = f"{DATA_API}/activity"
        params = {"user": wallet, "type": "TRADE", "limit": 1}
        try:
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()
            trades = resp.json()
            if isinstance(trades, list) and trades:
                return _parse_timestamp(trades[0])
            return 0.0
        except requests.RequestException as exc:
            logger.debug("Activity API failed for %s: %s", wallet[:10], exc)
            return 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Metric computation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_win_rate(closed_positions: List[dict]) -> Tuple[float, int, int]:
        """
        Compute win rate from closed position data.

        A position is a "win" if its current value (at close) exceeds its
        cost basis, i.e. the trader made a profit.

        Returns:
            Tuple of (win_rate, wins, total).
        """
        if not closed_positions:
            return 0.0, 0, 0

        wins = 0
        total = 0

        for pos in closed_positions:
            # Polymarket closed-positions API fields
            # "value" is the realised value at close; "initialValue" / "buyCost" is cost
            value = float(
                pos.get("value")
                or pos.get("currentValue")
                or pos.get("closeValue")
                or 0
            )
            cost = float(
                pos.get("initialValue")
                or pos.get("buyCost")
                or pos.get("cost")
                or 0
            )

            # If explicit pnl is available, use it directly
            pnl = pos.get("pnl") or pos.get("realizedPnl")
            if pnl is not None:
                try:
                    if float(pnl) > 0:
                        wins += 1
                    total += 1
                    continue
                except (TypeError, ValueError):
                    pass

            if cost > 0:
                total += 1
                if value > cost:
                    wins += 1
            elif value > 0:
                # No cost info but we have a value — assume break-even as a loss
                total += 1

        win_rate = wins / total if total > 0 else 0.0
        return win_rate, wins, total


def _parse_timestamp(trade: dict) -> float:
    """
    Extract a Unix timestamp (seconds) from an activity record.

    Handles multiple common field names and both seconds and milliseconds.
    """
    for key in ("timestamp", "createdAt", "created_at", "time", "ts"):
        val = trade.get(key)
        if val:
            try:
                ts = float(val)
                # Milliseconds if value is larger than year 2100 in seconds
                if ts > 4_102_444_800:
                    ts /= 1000.0
                return ts
            except (TypeError, ValueError):
                continue
    return 0.0
