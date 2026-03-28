"""
trade_manager.py
----------------
Actively manages open positions to enforce short-term trading discipline.

Responsibilities:
  • Stop-loss:   Close positions when unrealised P&L drops below -STOP_LOSS_PCT.
  • Trailing stop: Once a position gains TRAILING_STOP_ACTIVATION%, lock in
    profits by selling if price retraces TRAILING_STOP_PCT from its peak.
  • Take-profit: Close positions when unrealised P&L exceeds TAKE_PROFIT_PCT.
  • Time exit:   Close positions open longer than MAX_HOLD_TIME seconds.

Exit priority order per cycle:
    stop-loss > trailing stop > take-profit > time exit

All exits generate a TradeSignal(side="SELL") executed through the Executor.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from config import Config
from execution import Executor, ExecutionResult
from market_scanner import classify_market
from position_tracker import Position, PositionTracker
from strategies.base import TradeSignal

if TYPE_CHECKING:
    from market_scanner import MarketScanner
    from ai_probability_engine import AIProbabilityEngine

logger = logging.getLogger("bot.trade_manager")


@dataclass
class PositionMeta:
    """
    Per-position tracking data maintained by TradeManager.

    Stores the high-water-mark price (for trailing stops) and whether the
    trailing stop has been activated for a given position.
    """

    token_id: str
    high_water_mark: float = 0.0         # Highest price seen since position opened
    trailing_stop_active: bool = False    # True once TRAILING_STOP_ACTIVATION is hit
    trailing_stop_price: float = 0.0     # Price at which trailing stop triggers
    exit_attempted: bool = False          # Guard against double-exit attempts
    partial_exit_done: bool = False      # True after the 50% scale-out at +10%
    original_size: float = 0.0           # Size at position open (before partial exit)


class TradeManager:
    """
    Monitors open positions each cycle and applies exit rules.

    Usage:
        manager = TradeManager(tracker, executor, config, scanner)
        # Called every cycle after strategies have been scanned:
        manager.manage_positions()
    """

    def __init__(
        self,
        tracker: PositionTracker,
        executor: Executor,
        cfg: Config,
        market_scanner: Optional["MarketScanner"] = None,
        ai_engine: Optional["AIProbabilityEngine"] = None,
    ) -> None:
        self.tracker = tracker
        self.executor = executor
        self.cfg = cfg
        self.market_scanner = market_scanner
        self.ai_engine = ai_engine

        # token_id -> PositionMeta for trailing stop tracking
        self._meta: Dict[str, PositionMeta] = {}

        # Bayesian re-evaluation cycle counter
        self._cycle_count: int = 0

        # Cache of AI probability estimates for open positions
        self._ai_estimates: Dict[str, float] = {}  # market_id -> last probability

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def manage_positions(self) -> List[ExecutionResult]:
        """
        Evaluate all open positions and execute exits where triggered.

        Should be called once per bot cycle, after strategy scans and after
        position prices have been refreshed by PositionTracker.refresh().

        Returns:
            List of ExecutionResult objects for any exits attempted.
        """
        self._cycle_count += 1

        positions = [p for p in self.tracker.get_all_positions() if not p.resolved]

        if not positions:
            logger.debug("TradeManager: no open positions to manage.")
            return []

        # Bayesian re-evaluation every N cycles (for AI-powered positions)
        if (
            self.ai_engine
            and self.ai_engine.enabled
            and self._cycle_count % self.cfg.REEVALUATE_INTERVAL == 0
        ):
            self._bayesian_reevaluate(positions)

        results: List[ExecutionResult] = []

        for pos in positions:
            try:
                result = self._evaluate_position(pos)
                if result:
                    results.append(result)
            except Exception as exc:
                logger.error(
                    "TradeManager error evaluating %s: %s",
                    pos.token_id[:16],
                    exc,
                    exc_info=True,
                )

        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_meta(self, pos: Position) -> PositionMeta:
        """Return (or create) the PositionMeta for a position."""
        if pos.token_id not in self._meta:
            self._meta[pos.token_id] = PositionMeta(
                token_id=pos.token_id,
                high_water_mark=pos.current_price or pos.entry_price,
            )
        return self._meta[pos.token_id]

    # Strategies that always hold to resolution regardless of config
    ALWAYS_HOLD_TO_RESOLUTION = {"crypto_mean_reversion", "weather_forecast_arb"}

    # Categories where prices swing wildly during live events.
    # These get aggressive trailing stops and should NEVER hold to resolution.
    LIVE_EVENT_CATEGORIES = {"sports", "esports"}

    # Aggressive trailing stop for live events: activate at +20% gain,
    # trail by 15% from the high-water mark. This catches situations like
    # the Spartans game where price hit 50¢ from 14¢ entry then crashed.
    LIVE_EVENT_TRAIL_ACTIVATION = 0.20   # Activate after +20% unrealised gain
    LIVE_EVENT_TRAIL_PCT = 0.15          # 15% trailing stop from HWM

    # Partial exit: sell this fraction at the first profit target
    PARTIAL_EXIT_FRACTION = 0.50   # Sell 50%
    PARTIAL_EXIT_TRIGGER = 0.10    # At +10% PnL

    # Base rate estimates by category (v34)
    # Rough historical base rates for how often "Yes" outcomes resolve true
    _CATEGORY_BASE_RATES = {
        "crypto": 0.45,       # Roughly 50/50 (up/down markets)
        "sports": 0.35,       # Underdogs rarely win
        "esports": 0.30,      # Even more volatile
        "politics": 0.25,     # Incumbents/favorites dominate
        "geopolitics": 0.15,  # Rare events
        "finance": 0.20,      # Most predictions fail
        "weather": 0.40,      # Forecasts are decent
        "entertainment": 0.25,
        "economics": 0.20,
        "other": 0.30,
    }

    def _evaluate_position(self, pos: Position) -> Optional[ExecutionResult]:
        """
        Run exit checks for a single position in priority order.

        Priority: stop-loss > trailing stop > partial scale-out > take-profit > time exit.

        Crypto mean-reversion positions are skipped (hold to resolution).
        """
        meta = self._get_meta(pos)

        # Guard: don't attempt to exit the same position twice in one cycle
        if meta.exit_attempted:
            return None

        current_price = pos.current_price
        if current_price <= 0:
            logger.debug(
                "TradeManager: no current price for %s, skipping.", pos.token_id[:16]
            )
            return None

        # Track original size for partial exit calculation
        if meta.original_size <= 0:
            meta.original_size = pos.size

        # Update high-water-mark
        if current_price > meta.high_water_mark:
            meta.high_water_mark = current_price
            logger.debug(
                "New HWM for %s: %.4f", pos.token_id[:16], meta.high_water_mark
            )

        # Update trailing stop activation and trigger price
        self._update_trailing_stop(pos, meta)

        pnl_pct = pos.unrealised_pnl_pct / 100.0   # convert to decimal

        # ── 1. Stop-loss (full exit) ───────────────────────────────────────
        if pnl_pct <= -self.cfg.STOP_LOSS_PCT:
            return self._exit_position(
                pos,
                meta,
                reason=(
                    f"Stop-loss triggered: pnl={pnl_pct:.2%} "
                    f"<= -{self.cfg.STOP_LOSS_PCT:.2%}"
                ),
                order_type="FOK",
            )

        # ── 2. Trailing stop (closes remaining position) ──────────────────
        if meta.trailing_stop_active and current_price <= meta.trailing_stop_price:
            return self._exit_position(
                pos,
                meta,
                reason=(
                    f"Trailing stop hit: price={current_price:.4f} "
                    f"<= stop={meta.trailing_stop_price:.4f} "
                    f"(hwm={meta.high_water_mark:.4f})"
                ),
                order_type="FOK",
            )

        # ── 3. Partial scale-out: sell 50% at +10% ───────────────────────
        if not meta.partial_exit_done and pnl_pct >= self.PARTIAL_EXIT_TRIGGER:
            partial_size = round(pos.size * self.PARTIAL_EXIT_FRACTION, 4)
            if partial_size >= 1.0:  # Only if meaningful size to sell
                result = self._exit_position(
                    pos,
                    meta,
                    reason=(
                        f"Partial scale-out: pnl={pnl_pct:.2%} >= "
                        f"{self.PARTIAL_EXIT_TRIGGER:.0%} | "
                        f"selling {self.PARTIAL_EXIT_FRACTION:.0%} "
                        f"({partial_size:.1f} of {pos.size:.1f} shares)"
                    ),
                    order_type="GTC",
                    size_override=partial_size,
                )
                if result and result.success:
                    meta.partial_exit_done = True
                    # Allow further exits — don't keep exit_attempted True
                    meta.exit_attempted = False
                    logger.info(
                        "Partial exit done for %s: sold %.1f, %.1f remaining",
                        pos.token_id[:16],
                        partial_size,
                        pos.size - partial_size,
                    )
                return result

        # ── Detect live event categories (sports/esports) ─────────────────
        strategy_name = getattr(pos, 'strategy', '') or ''
        market_category = self._detect_category(pos)
        is_live_event = market_category in self.LIVE_EVENT_CATEGORIES

        # ── Live event aggressive trailing stop ───────────────────────────
        # Sports/esports prices swing wildly during games. If we're up 20%+,
        # activate a tight 15% trailing stop to lock in profits.
        # This prevents the Spartans scenario: bought at 14¢, hit 50¢, crashed to 2.5¢.
        if is_live_event and pnl_pct >= self.LIVE_EVENT_TRAIL_ACTIVATION:
            trail_price = meta.high_water_mark * (1.0 - self.LIVE_EVENT_TRAIL_PCT)
            if current_price <= trail_price:
                return self._exit_position(
                    pos,
                    meta,
                    reason=(
                        f"Live event trailing stop: price={current_price:.4f} "
                        f"<= trail={trail_price:.4f} "
                        f"(hwm={meta.high_water_mark:.4f}, entry={pos.entry_price:.4f}, "
                        f"gain_was={pnl_pct:.0%}, cat={market_category})"
                    ),
                    order_type="FOK",
                )

        # ── v34: Hold-to-resolution check ──────────────────────────────────
        # When HOLD_TO_RESOLUTION is enabled, skip TP/time exits unless EV
        # has flipped negative. NEVER hold sports/esports to resolution.
        is_hold_strategy = strategy_name in self.ALWAYS_HOLD_TO_RESOLUTION
        hold_override = (
            (self.cfg.HOLD_TO_RESOLUTION or is_hold_strategy)
            and not is_live_event                      # Never hold sports/esports
            and pnl_pct > -self.cfg.STOP_LOSS_PCT      # Not at stop-loss level
            and not self._ev_is_negative(pos)           # EV hasn't flipped
        )

        # ── 4. Take-profit (full exit at +15% for remaining shares) ──────
        if pnl_pct >= self.cfg.TAKE_PROFIT_PCT and not hold_override:
            return self._exit_position(
                pos,
                meta,
                reason=(
                    f"Take-profit: pnl={pnl_pct:.2%} "
                    f">= {self.cfg.TAKE_PROFIT_PCT:.2%}"
                ),
                order_type="GTC",
            )

        # ── 5. Time-based exit ────────────────────────────────────────────
        age_seconds = time.time() - pos.opened_at
        if age_seconds >= self.cfg.MAX_HOLD_TIME and not hold_override:
            return self._exit_position(
                pos,
                meta,
                reason=(
                    f"Time exit: position age={age_seconds / 3600:.1f}h "
                    f">= max={self.cfg.MAX_HOLD_TIME / 3600:.1f}h"
                ),
                order_type="GTC",
            )

        logger.debug(
            "Position %s OK | pnl=%.2f%% | age=%.1fh | hwm=%.4f | trailing=%s | partial=%s",
            pos.token_id[:16],
            pnl_pct * 100,
            age_seconds / 3600,
            meta.high_water_mark,
            meta.trailing_stop_active,
            meta.partial_exit_done,
        )
        return None

    def _update_trailing_stop(self, pos: Position, meta: PositionMeta) -> None:
        """
        Activate the trailing stop once a position has gained enough, and
        continuously update the trailing stop price as price rises.
        """
        gain_pct = (pos.current_price - pos.entry_price) / pos.entry_price if pos.entry_price > 0 else 0.0

        if not meta.trailing_stop_active:
            if gain_pct >= self.cfg.TRAILING_STOP_ACTIVATION:
                meta.trailing_stop_active = True
                meta.trailing_stop_price = meta.high_water_mark * (
                    1.0 - self.cfg.TRAILING_STOP_PCT
                )
                logger.info(
                    "Trailing stop ACTIVATED for %s | "
                    "gain=%.2f%% | hwm=%.4f | stop=%.4f",
                    pos.token_id[:16],
                    gain_pct * 100,
                    meta.high_water_mark,
                    meta.trailing_stop_price,
                )
        else:
            # Ratchet the stop up when a new HWM is set
            new_stop = meta.high_water_mark * (1.0 - self.cfg.TRAILING_STOP_PCT)
            if new_stop > meta.trailing_stop_price:
                meta.trailing_stop_price = new_stop
                logger.debug(
                    "Trailing stop ratcheted for %s | stop=%.4f",
                    pos.token_id[:16],
                    meta.trailing_stop_price,
                )

    def _exit_position(
        self,
        pos: Position,
        meta: PositionMeta,
        reason: str,
        order_type: str = "GTC",
        size_override: Optional[float] = None,
    ) -> Optional[ExecutionResult]:
        """
        Build a SELL TradeSignal and execute it, recording the attempt.

        Args:
            pos:        The Position to close.
            meta:       The PositionMeta for this position.
            reason:     Human-readable exit reason for logs and history.
            order_type: "GTC" for limit, "FOK" for market/immediate.

        Returns:
            ExecutionResult from the executor, or None if execution fails.
        """
        meta.exit_attempted = True

        # Use current market price, falling back to entry price
        sell_price = pos.current_price if pos.current_price > 0 else pos.entry_price

        exit_size = size_override if size_override is not None else pos.size
        sell_signal = TradeSignal(
            strategy="trade_manager",
            market_id=pos.market_id,
            token_id=pos.token_id,
            side="SELL",
            price=round(sell_price, 4),
            size=round(exit_size, 4),
            confidence=1.0,    # exits are always high-confidence
            reason=reason,
            order_type=order_type,
        )

        logger.info(
            "EXIT [%s]: token=%s | side=%s | size=%.4f @ $%.4f | reason=%s",
            order_type,
            pos.token_id[:16],
            "SELL",
            exit_size,
            sell_price,
            reason,
        )

        try:
            result = self.executor.execute(sell_signal)
            if result.success:
                logger.info(
                    "Exit executed: %s | order_id=%s",
                    pos.token_id[:16],
                    result.order_id,
                )
                # Remove tracking meta so position isn't managed after exit
                self._meta.pop(pos.token_id, None)
            else:
                logger.warning(
                    "Exit order rejected for %s: %s",
                    pos.token_id[:16],
                    result.error,
                )
            return result
        except Exception as exc:
            logger.error(
                "Failed to submit exit for %s: %s",
                pos.token_id[:16],
                exc,
                exc_info=True,
            )
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # v34: Base rate, hold-to-resolution, Bayesian re-evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_category(self, pos: Position) -> str:
        """
        Detect the market category for a position.

        Uses the market scanner to look up the question text, then classifies it.
        Falls back to 'other' if the market isn't in the scanner cache.
        """
        if not self.market_scanner:
            return "other"
        market = self.market_scanner.get_market(pos.market_id)
        if not market:
            return "other"
        return classify_market(market.question)

    def estimate_category_base_rate(self, market_category: str) -> float:
        """
        Return a rough base rate for a market category.

        Used to check if a trade's category has a sufficiently high base rate
        before entering.
        """
        return self._CATEGORY_BASE_RATES.get(market_category, 0.30)

    def apply_base_rate_sizing(self, signal: TradeSignal) -> TradeSignal:
        """
        Apply base-rate rules to a trade signal before execution.

        If the event category's historical base rate is below BASE_RATE_MIN,
        reduce position size by BASE_RATE_SIZE_CUT.

        Returns the (possibly modified) signal.
        """
        # Determine market category
        category = "other"
        if self.market_scanner:
            market = self.market_scanner.get_market(signal.market_id)
            if market:
                category = classify_market(market.question)

        base_rate = self.estimate_category_base_rate(category)

        if base_rate < self.cfg.BASE_RATE_MIN:
            original_size = signal.size
            signal.size = round(signal.size * (1.0 - self.cfg.BASE_RATE_SIZE_CUT), 4)
            logger.info(
                "Base rate cut: category=%s rate=%.0f%% < %.0f%% | size %.2f -> %.2f",
                category, base_rate * 100, self.cfg.BASE_RATE_MIN * 100,
                original_size, signal.size,
            )

        return signal

    def _ev_is_negative(self, pos: Position) -> bool:
        """
        Check if the Bayesian-updated EV for a position has turned negative.

        Uses the cached AI estimate to compute EV.  If no estimate is cached,
        returns False (don't exit).
        """
        est_prob = self._ai_estimates.get(pos.market_id)
        if est_prob is None:
            return False

        # EV = prob * ($1 - entry) - (1 - prob) * entry
        ev = est_prob * (1.0 - pos.entry_price) - (1.0 - est_prob) * pos.entry_price
        return ev < 0

    def _bayesian_reevaluate(self, positions: List[Position]) -> None:
        """
        Periodically re-evaluate AI-powered positions using Bayesian update.

        If the updated probability flips the EV to negative, mark the
        position for exit on the next check.
        """
        if not self.ai_engine or not self.market_scanner:
            return

        for pos in positions:
            market = self.market_scanner.get_market(pos.market_id)
            if not market:
                continue

            prior = self._ai_estimates.get(pos.market_id, pos.entry_price)
            try:
                updated = self.ai_engine.reevaluate_position(market, prior)
                if updated is not None:
                    self._ai_estimates[pos.market_id] = updated
                    ev = updated * (1.0 - pos.entry_price) - (1.0 - updated) * pos.entry_price
                    if ev < 0:
                        logger.info(
                            "Bayesian re-eval: EV flipped negative for %s "
                            "(prob=%.1f%% entry=%.3f EV=%.4f). Flagging for exit.",
                            pos.token_id[:16], updated * 100, pos.entry_price, ev,
                        )
            except Exception as exc:
                logger.debug("Bayesian re-eval failed for %s: %s", pos.market_id[:16], exc)
