"""
risk_manager.py
---------------
Pre-trade risk checks, position limits, daily P&L tracking, and kill switch.

The RiskManager acts as a gate between strategy signals and execution.
No trade should be sent to the Executor without first calling approve_trade().

Checks performed:
  1. Kill switch: if total P&L has breached KILL_SWITCH_THRESHOLD, block all trades.
  2. Position size: this trade would not exceed MAX_POSITION_SIZE.
  3. Total exposure: adding this position would not exceed MAX_TOTAL_EXPOSURE.
  4. Position count: we have not exceeded MAX_POSITIONS.
  5. Market liquidity: the market has MIN_LIQUIDITY volume.
"""

import datetime
import logging
import time
from typing import TYPE_CHECKING, Dict

from config import Config
from strategies.base import TradeSignal

if TYPE_CHECKING:
    from position_tracker import PositionTracker

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Evaluates TradeSignal objects against configured risk limits.

    The RiskManager does not own position data directly; it queries a
    PositionTracker for current exposure figures.

    Usage:
        rm = RiskManager(config, position_tracker)
        if rm.approve_trade(signal):
            executor.execute(signal)
    """

    def __init__(self, cfg: Config, position_tracker: "PositionTracker") -> None:
        self.cfg = cfg
        self.tracker = position_tracker
        self._kill_switch_active = False
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: str = ""  # Track which UTC date we're on
        self._consecutive_losses: int = 0
        self._consecutive_loss_pause_until: float = 0.0
        # Rejection counter for monitoring
        self._rejections: Dict[str, int] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def approve_trade(self, signal: TradeSignal) -> bool:
        """
        Run all risk checks on a signal.

        Returns:
            True if the trade is approved, False if it should be skipped.
        """
        # ── Kill switch ─────────────────────────────────────────────────────
        if self._kill_switch_active:
            logger.warning(
                "KILL SWITCH ACTIVE — all trading halted. Signal rejected: %s",
                signal,
            )
            return False

        # ── Check daily P&L against threshold ──────────────────────────────
        self._check_kill_switch()
        if self._kill_switch_active:
            return False

        # ── Daily drawdown circuit breaker ─────────────────────────────────
        self._maybe_reset_daily_pnl()
        estimated_bankroll = max(self.tracker.total_exposure() + 50, 100)
        if self._daily_pnl <= -(estimated_bankroll * self.cfg.MAX_DAILY_DRAWDOWN_PCT):
            self._reject(
                signal,
                f"daily_drawdown: daily_pnl=${self._daily_pnl:.2f} exceeds "
                f"{self.cfg.MAX_DAILY_DRAWDOWN_PCT:.0%} of bankroll=${estimated_bankroll:.0f}",
            )
            return False

        # ── Consecutive loss cooldown ───────────────────────────────────────
        if time.time() < self._consecutive_loss_pause_until:
            self._reject(
                signal,
                "consecutive loss cooldown",
            )
            return False

        trade_usd = signal.price * signal.size

        # ── Position size check ─────────────────────────────────────────────
        if trade_usd > self.cfg.MAX_POSITION_SIZE:
            self._reject(
                signal,
                f"trade_usd=${trade_usd:.2f} > MAX_POSITION_SIZE=${self.cfg.MAX_POSITION_SIZE}",
            )
            return False

        # ── Total exposure check ────────────────────────────────────────────
        current_exposure = self.tracker.total_exposure()
        if current_exposure + trade_usd > self.cfg.MAX_TOTAL_EXPOSURE:
            self._reject(
                signal,
                f"total_exposure=${current_exposure + trade_usd:.2f} would exceed "
                f"MAX_TOTAL_EXPOSURE=${self.cfg.MAX_TOTAL_EXPOSURE}",
            )
            return False

        # ── Position count check ────────────────────────────────────────────
        n_positions = self.tracker.position_count()
        if n_positions >= self.cfg.MAX_POSITIONS:
            self._reject(
                signal,
                f"position_count={n_positions} >= MAX_POSITIONS={self.cfg.MAX_POSITIONS}",
            )
            return False

        # ── Liquidity check ─────────────────────────────────────────────────
        market = None
        try:
            market = self.tracker.market_scanner.get_market(signal.market_id)
        except AttributeError:
            pass  # tracker may not have a reference to scanner in all configs

        if market:
            liq = max(market.volume, market.liquidity)
            if liq < self.cfg.MIN_LIQUIDITY:
                self._reject(
                    signal,
                    f"market_liquidity=${liq:.0f} < MIN_LIQUIDITY=${self.cfg.MIN_LIQUIDITY}",
                )
                return False

        # ── Correlation-aware position limit ────────────────────────────────
        if not self._check_correlated_positions(signal, market):
            self._reject(
                signal,
                "correlated_positions: too many open positions in same category",
            )
            return False

        logger.debug(
            "Trade approved: %s | $%.2f | exposure_after=$%.2f",
            signal.strategy,
            trade_usd,
            current_exposure + trade_usd,
        )
        return True

    def record_trade_result(self, pnl: float) -> None:
        """
        Record the outcome of a completed trade.

        Updates daily P&L, tracks consecutive losses, and triggers the
        consecutive-loss pause if needed.

        Args:
            pnl: Realised P&L for this trade (positive=profit, negative=loss).
        """
        self._maybe_reset_daily_pnl()
        self._daily_pnl += pnl
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.cfg.MAX_CONSECUTIVE_LOSSES:
                self._consecutive_loss_pause_until = time.time() + 900  # 15 min
                logger.warning(
                    "Consecutive loss limit reached (%d losses). "
                    "Pausing trading for 15 minutes.",
                    self._consecutive_losses,
                )
        else:
            self._consecutive_losses = 0
        self._check_kill_switch()

    def _maybe_reset_daily_pnl(self) -> None:
        """Reset daily P&L if the UTC date has rolled over."""
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._daily_pnl_date:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today

    def update_pnl(self, delta: float) -> None:
        """
        Update the running daily P&L.  Call after each trade is confirmed.

        Args:
            delta: Change in P&L (positive = profit, negative = loss).
        """
        self._daily_pnl += delta
        logger.debug("Daily P&L updated: $%.2f (delta=$%.2f)", self._daily_pnl, delta)
        self._check_kill_switch()

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L counter (call at the start of each trading day)."""
        logger.info("Daily P&L reset from $%.2f to $0.00", self._daily_pnl)
        self._daily_pnl = 0.0

    def activate_kill_switch(self, reason: str = "manual") -> None:
        """Manually activate the kill switch."""
        self._kill_switch_active = True
        logger.critical("KILL SWITCH MANUALLY ACTIVATED: %s", reason)

    def deactivate_kill_switch(self) -> None:
        """Deactivate the kill switch (use with caution)."""
        self._kill_switch_active = False
        logger.warning("Kill switch deactivated.")

    @property
    def kill_switch_active(self) -> bool:
        return self._kill_switch_active

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def rejection_summary(self) -> dict:
        """Return a dict of rejection reason -> count for monitoring."""
        return dict(self._rejections)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────────────────────────────────────

    def _check_correlated_positions(self, signal: TradeSignal, market=None) -> bool:
        """
        Check if too many open positions are in the same category.

        If 3+ positions are in the same market category as this signal,
        the allowed position count is effectively reduced by 30%.

        Args:
            signal: The incoming TradeSignal.
            market: Optional market object (for category extraction).

        Returns:
            True if the trade is allowed, False if it should be blocked.
        """
        import re

        # Try to get category of the signal's market
        signal_category = "general"
        try:
            if market and hasattr(market, "question"):
                signal_category = self._classify_market_question(market.question)
        except Exception:
            pass

        if signal_category == "general":
            return True  # Can't classify, skip correlation check

        # Count open positions in the same category
        same_category_count = 0
        try:
            all_positions = self.tracker.get_all_positions()
            for pos in all_positions:
                if pos.resolved:
                    continue
                try:
                    pos_market = self.tracker.market_scanner.get_market(pos.market_id)
                    if pos_market and hasattr(pos_market, "question"):
                        pos_cat = self._classify_market_question(pos_market.question)
                        if pos_cat == signal_category:
                            same_category_count += 1
                except Exception:
                    pass
        except Exception:
            return True  # If we can't check, allow the trade

        # Apply stricter limit: if 3+ positions in same category, block
        category_limit = max(int(self.cfg.MAX_POSITIONS * 0.70), 1)
        if same_category_count >= 3 and same_category_count >= category_limit:
            logger.info(
                "Correlated positions: %d open %s positions (limit=%d)",
                same_category_count, signal_category, category_limit,
            )
            return False
        return True

    @staticmethod
    def _classify_market_question(question: str) -> str:
        """Classify a market question into a broad category using keyword matching."""
        import re as _re
        q = question.lower()
        category_keywords = {
            "politics": [
                r"\b(president|congress|senate|election|vote|bill|law|democrat|republican)\b",
            ],
            "crypto": [
                r"\b(bitcoin|ethereum|crypto|btc|eth|solana|blockchain)\b",
            ],
            "sports": [
                r"\b(nba|nfl|mlb|nhl|championship|playoffs|finals|match|game|team)\b",
            ],
            "finance": [
                r"\b(fed|interest rate|inflation|gdp|stock|nasdaq|s&p|earnings)\b",
            ],
            "geopolitics": [
                r"\b(war|conflict|nato|sanctions|russia|ukraine|china|iran)\b",
            ],
            "weather": [
                r"\b(weather|temperature|hurricane|storm|flood|forecast)\b",
            ],
        }
        for category, patterns in category_keywords.items():
            for pattern in patterns:
                if _re.search(pattern, q, _re.IGNORECASE):
                    return category
        return "general"

    def _check_kill_switch(self) -> None:
        """
        Automatically activate the kill switch if daily P&L has breached
        the configured threshold.
        """
        if not self._kill_switch_active and self._daily_pnl <= self.cfg.KILL_SWITCH_THRESHOLD:
            self._kill_switch_active = True
            logger.critical(
                "KILL SWITCH TRIGGERED: daily_pnl=$%.2f <= threshold=$%.2f. "
                "All trading halted.",
                self._daily_pnl,
                self.cfg.KILL_SWITCH_THRESHOLD,
            )

    def _reject(self, signal: TradeSignal, reason: str) -> None:
        """Log a rejection and increment the rejection counter."""
        key = reason.split("$")[0].strip()  # normalise key
        self._rejections[key] = self._rejections.get(key, 0) + 1
        logger.info(
            "Trade rejected [%s]: %s | reason: %s",
            signal.strategy,
            signal.token_id[:16],
            reason,
        )
