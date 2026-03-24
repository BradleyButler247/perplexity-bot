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

import logging
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

        logger.debug(
            "Trade approved: %s | $%.2f | exposure_after=$%.2f",
            signal.strategy,
            trade_usd,
            current_exposure + trade_usd,
        )
        return True

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

    def rejection_summary(self) -> dict:
        """Return a dict of rejection reason -> count for monitoring."""
        return dict(self._rejections)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────────────────────────────────────

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
