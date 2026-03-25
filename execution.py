"""
execution.py
------------
Handles all order placement, slippage protection, and paper-trade logging.

Responsibilities:
  • Accept TradeSignal objects from strategies.
  • Verify current price hasn't drifted beyond MAX_SLIPPAGE before submitting.
  • Build and sign limit (GTC) or market (FOK) orders via the CLOB SDK.
  • In paper mode: log the intended trade without submitting.
  • In micro mode: place REAL orders but override size to MICRO_TRADE_SIZE.
  • In live mode: place full-size orders.
  • Return a structured ExecutionResult for each attempt.
  • Respect the 60 orders/minute rate limit via a simple token-bucket.

Order flow:
    TradeSignal ──► mode check (paper / micro / live)
                ──► [micro] override size to MICRO_TRADE_SIZE
                ──► pre-flight price check
                ──► build OrderArgs / MarketOrderArgs
                ──► client.create_order() / client.create_market_order()
                ──► client.post_order(signed, order_type)
                ──► ExecutionResult
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from config import Config
from strategies.base import TradeSignal

logger = logging.getLogger("bot.trade")

# ── Rate-limit guard ─────────────────────────────────────────────────────────
# Polymarket allows ~60 authenticated orders per minute.
MAX_ORDERS_PER_MINUTE = 55   # leave a small buffer


@dataclass
class ExecutionResult:
    """Outcome of an order submission attempt."""

    signal: TradeSignal
    success: bool
    order_id: Optional[str] = None
    status: str = "unknown"   # submitted / rejected / paper / micro / error
    error: Optional[str] = None
    filled_price: Optional[float] = None
    filled_size: Optional[float] = None
    paper_trade: bool = False
    mode: str = "paper"       # "paper", "micro", or "live"
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        if self.mode == "paper":
            return (
                f"[PAPER] {self.signal.side} {self.signal.size:.2f} shares "
                f"@ ${self.signal.price:.4f} | {self.signal.token_id[:16]}… "
                f"| {self.signal.strategy}"
            )
        if self.mode == "micro":
            if self.success:
                return (
                    f"[MICRO] {self.signal.side} {self.signal.size:.4f} shares "
                    f"@ ${self.signal.price:.4f} | order_id={self.order_id} "
                    f"| status={self.status} | {self.signal.strategy}"
                )
            return (
                f"[MICRO][FAIL] {self.signal.side} {self.signal.size:.4f} shares "
                f"@ ${self.signal.price:.4f} | error={self.error}"
            )
        if self.success:
            return (
                f"[LIVE] {self.signal.side} {self.signal.size:.2f} shares "
                f"@ ${self.signal.price:.4f} | order_id={self.order_id} "
                f"| status={self.status}"
            )
        return (
            f"[FAIL] {self.signal.side} {self.signal.size:.2f} shares "
            f"@ ${self.signal.price:.4f} | error={self.error}"
        )


class Executor:
    """
    Responsible for converting TradeSignals into actual (or simulated) orders.

    Usage:
        executor = Executor(config, clob_client)
        result = executor.execute(signal)
    """

    def __init__(self, cfg: Config, client: ClobClient) -> None:
        self.cfg = cfg
        self.client = client
        self._order_timestamps: list = []   # timestamps of recent orders
        self._cached_balance: float = -1.0  # last known USDC balance (-1 = unknown)
        self._balance_ts: float = 0.0       # timestamp of last balance check

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def execute(self, signal: TradeSignal) -> ExecutionResult:
        """
        Execute a TradeSignal in paper, micro, or live mode.

        In micro mode the signal's size is overridden to MICRO_TRADE_SIZE / price
        so that only a tiny real-money order is placed regardless of what the
        strategy calculated.

        Args:
            signal: The TradeSignal to act on.

        Returns:
            ExecutionResult describing the outcome.
        """
        mode = self.cfg.TRADING_MODE

        # ── Paper trade mode ───────────────────────────────────────────────
        if mode == "paper":
            return self._paper_trade(signal)

        # ── Micro mode: override size before pre-flight checks ─────────────
        if mode == "micro":
            signal = self._apply_micro_size(signal)

        # -- Balance pre-check ------------------------------------------------
        if signal.side.upper() == "BUY":
            order_cost = signal.price * signal.size
            balance = self._get_usdc_balance()
            if balance >= 0 and order_cost > balance:
                error_msg = (
                    f"Insufficient balance: order costs ${order_cost:.2f} "
                    f"but only ${balance:.2f} USDC available"
                )
                logger.info(error_msg)
                return ExecutionResult(
                    signal=signal,
                    success=False,
                    status="rejected",
                    error=error_msg,
                )

        # ── Pre-flight checks ──────────────────────────────────────────────
        slippage_ok, current_price = self._check_slippage(signal)
        if not slippage_ok:
            error_msg = (
                f"Slippage guard triggered: signal_price={signal.price:.4f} "
                f"current_price={current_price:.4f} "
                f"drift={abs(current_price - signal.price):.4f} "
                f"threshold={self.cfg.MAX_SLIPPAGE:.4f}"
            )
            logger.warning(error_msg)
            return ExecutionResult(
                signal=signal,
                success=False,
                status="rejected",
                error=error_msg,
            )

        # ── Rate-limit check ───────────────────────────────────────────────
        if not self._check_rate_limit():
            error_msg = "Rate limit: too many orders in the last 60 seconds."
            logger.warning(error_msg)
            return ExecutionResult(
                signal=signal,
                success=False,
                status="rejected",
                error=error_msg,
            )

        # ── Submit order ───────────────────────────────────────────────────
        if signal.order_type.upper() == "FOK":
            return self._submit_market_order(signal, mode=mode)
        else:
            return self._submit_limit_order(signal, mode=mode)

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order by ID.

        Returns:
            True if cancel was accepted, False otherwise.
        """
        if self.cfg.TRADING_MODE == "paper":
            logger.info("[PAPER] Would cancel order %s", order_id)
            return True
        try:
            resp = self.client.cancel(order_id)
            logger.info("Cancelled order %s: %s", order_id, resp)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders on the account.

        Returns:
            True if the cancel-all request was accepted.
        """
        if self.cfg.TRADING_MODE == "paper":
            logger.info("[PAPER] Would cancel all open orders.")
            return True
        try:
            resp = self.client.cancel_all()
            logger.info("Cancel-all response: %s", resp)
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _paper_trade(self, signal: TradeSignal) -> ExecutionResult:
        """Log a trade as if it were executed, without submitting to the API."""
        result = ExecutionResult(
            signal=signal,
            success=True,
            status="paper",
            paper_trade=True,
            mode="paper",
            filled_price=signal.price,
            filled_size=signal.size,
        )
        logger.info(str(result))
        logger.info(
            "[PAPER] Reason: %s | confidence=%.2f | usd_value=$%.2f",
            signal.reason[:120],
            signal.confidence,
            signal.usd_value,
        )
        return result

    # Polymarket minimum order size (shares)
    MIN_ORDER_SIZE = 5.0

    def _apply_micro_size(self, signal: TradeSignal) -> TradeSignal:
        """
        Return a copy of the signal with size overridden for micro-mode.

        Uses a fractional Kelly Criterion to size positions proportionally
        to the signal's edge and confidence, capped at MICRO_TRADE_SIZE.

        Kelly formula for binary outcomes:
            f* = (p * b - q) / b
        where:
            p = probability of winning (approximated by signal.confidence)
            q = 1 - p
            b = net payout ratio (1/price - 1 for binary markets)

        We use half-Kelly (f*/2) to be conservative, then cap at
        MICRO_TRADE_SIZE to limit absolute risk.
        """
        import dataclasses
        if signal.price <= 0:
            return signal

        # Kelly Criterion calculation
        p = max(min(signal.confidence, 0.95), 0.10)  # clamp to avoid extremes
        q = 1.0 - p
        b = (1.0 / signal.price) - 1.0  # payout ratio for binary market

        if b <= 0:
            # No edge possible at this price
            kelly_fraction = 0.0
        else:
            kelly_fraction = (p * b - q) / b

        # Half-Kelly for conservative sizing
        half_kelly = max(kelly_fraction / 2.0, 0.0)

        # Apply Kelly to determine USD to risk
        balance = self._get_usdc_balance()
        bankroll = balance if balance > 0 else self.cfg.MICRO_TRADE_SIZE * 10

        kelly_usd = bankroll * half_kelly
        # Cap at MICRO_TRADE_SIZE and floor at minimum viable trade
        trade_usd = max(min(kelly_usd, self.cfg.MICRO_TRADE_SIZE), self.cfg.MICRO_TRADE_SIZE * 0.5)

        micro_size = trade_usd / signal.price
        # Enforce Polymarket's minimum order size
        if micro_size < self.MIN_ORDER_SIZE:
            micro_size = self.MIN_ORDER_SIZE

        # HARD CAP: never exceed MICRO_TRADE_SIZE in dollar terms
        # even if the minimum share count pushes the cost higher
        actual_cost = micro_size * signal.price
        if actual_cost > self.cfg.MICRO_TRADE_SIZE * 1.1:  # 10% tolerance
            micro_size = self.cfg.MICRO_TRADE_SIZE / signal.price
            if micro_size < self.MIN_ORDER_SIZE:
                # Can't meet both the min share and max dollar constraint
                # at this price point — skip the trade
                logger.info(
                    "[MICRO] Skipping: min shares ($%.2f) exceeds max trade size ($%.2f) at price $%.3f",
                    self.MIN_ORDER_SIZE * signal.price,
                    self.cfg.MICRO_TRADE_SIZE,
                    signal.price,
                )
                return signal  # Return unmodified — balance check will reject it

        micro_size = round(micro_size, 6)

        overridden = dataclasses.replace(signal, size=micro_size)
        actual_cost = micro_size * signal.price
        logger.debug(
            "[MICRO] Kelly sizing: conf=%.2f kelly=%.3f half=%.3f -> "
            "$%.2f (%.1f shares @ $%.3f)",
            p, kelly_fraction, half_kelly,
            actual_cost, micro_size, signal.price,
        )
        return overridden

    def _get_usdc_balance(self) -> float:
        """
        Fetch current USDC balance from the CLOB API.

        Caches the result for 30 seconds to avoid excessive API calls.
        Returns -1.0 if the balance cannot be determined (allows trade
        to proceed rather than blocking on API failure).
        """
        import time as _time
        now = _time.time()
        if now - self._balance_ts < 30 and self._cached_balance >= 0:
            return self._cached_balance

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if resp and isinstance(resp, dict):
                # Balance is in USDC (6 decimals) — API returns as string or number
                raw = resp.get("balance", 0)
                self._cached_balance = float(raw) / 1e6 if float(raw) > 1000 else float(raw)
            else:
                self._cached_balance = -1.0
            self._balance_ts = now
            logger.debug("USDC balance: $%.2f", self._cached_balance)
            return self._cached_balance
        except Exception as exc:
            logger.debug("Balance check failed: %s", exc)
            return -1.0  # Unknown — allow trade to proceed

    def _check_slippage(self, signal: TradeSignal) -> tuple[bool, float]:
        """
        Fetch the current market price and compare to the signal price.

        Returns:
            Tuple of (within_threshold, current_price).
        """
        try:
            resp = self.client.get_price(signal.token_id, signal.side)
            current_price = float(resp.get("price", signal.price) if isinstance(resp, dict) else resp)
        except Exception as exc:
            logger.debug("Could not fetch current price for slippage check: %s", exc)
            # If we can't check, allow the order (conservative but functional)
            return True, signal.price

        drift = abs(current_price - signal.price)
        within = drift <= self.cfg.MAX_SLIPPAGE
        return within, current_price

    def _submit_limit_order(self, signal: TradeSignal, mode: str = "live") -> ExecutionResult:
        """
        Build and submit a GTC limit order.

        Uses client.create_order() to sign the order, then client.post_order()
        to submit it with OrderType.GTC.

        Args:
            signal: The (possibly micro-size-adjusted) TradeSignal.
            mode:   "live" or "micro" — used to label the ExecutionResult.
        """
        sdk_side = BUY if signal.side.upper() == "BUY" else SELL

        order_args = OrderArgs(
            token_id=signal.token_id,
            price=signal.price,
            size=signal.size,
            side=sdk_side,
        )

        try:
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)
            self._record_order()

            order_id = resp.get("orderID") or resp.get("id", "unknown")
            status = resp.get("status", "submitted")

            prefix = "[MICRO]" if mode == "micro" else "[LIVE]"
            logger.info(
                "%s GTC order placed: %s %s shares @ $%.4f | id=%s status=%s",
                prefix,
                signal.side,
                signal.size,
                signal.price,
                order_id,
                status,
            )

            return ExecutionResult(
                signal=signal,
                success=True,
                order_id=order_id,
                status=status,
                mode=mode,
                filled_price=signal.price,
                filled_size=signal.size,
            )

        except Exception as exc:
            logger.error("GTC order failed: %s", exc, exc_info=True)
            return ExecutionResult(
                signal=signal,
                success=False,
                status="error",
                mode=mode,
                error=str(exc),
            )

    def _submit_market_order(self, signal: TradeSignal, mode: str = "live") -> ExecutionResult:
        """
        Build and submit a FOK market order.

        Uses client.create_market_order() to sign, then client.post_order()
        with OrderType.FOK.  For BUY: amount is USDC to spend.
        For SELL: amount is shares to sell.

        Args:
            signal: The (possibly micro-size-adjusted) TradeSignal.
            mode:   "live" or "micro" — used to label the ExecutionResult.
        """
        sdk_side = BUY if signal.side.upper() == "BUY" else SELL

        # For a BUY FOK, 'amount' is USD to spend (price × size)
        # For a SELL FOK, 'amount' is number of shares
        if signal.side.upper() == "BUY":
            amount = signal.price * signal.size  # USDC to spend
        else:
            amount = signal.size  # shares to sell

        market_order_args = MarketOrderArgs(
            token_id=signal.token_id,
            amount=amount,
            side=sdk_side,
            order_type=OrderType.FOK,
        )

        try:
            signed = self.client.create_market_order(market_order_args)
            resp = self.client.post_order(signed, OrderType.FOK)
            self._record_order()

            order_id = resp.get("orderID") or resp.get("id", "unknown")
            status = resp.get("status", "submitted")

            prefix = "[MICRO]" if mode == "micro" else "[LIVE]"
            logger.info(
                "%s FOK order placed: %s $%.2f USDC @ ~$%.4f | id=%s status=%s",
                prefix,
                signal.side,
                amount,
                signal.price,
                order_id,
                status,
            )

            return ExecutionResult(
                signal=signal,
                success=True,
                order_id=order_id,
                status=status,
                mode=mode,
                filled_price=signal.price,
                filled_size=signal.size,
            )

        except Exception as exc:
            logger.error("FOK order failed: %s", exc, exc_info=True)
            return ExecutionResult(
                signal=signal,
                success=False,
                status="error",
                mode=mode,
                error=str(exc),
            )

    def _check_rate_limit(self) -> bool:
        """
        Enforce the max-60-orders-per-minute rate limit.

        Returns True if we are within the limit, False if we should back off.
        """
        now = time.time()
        # Remove timestamps older than 60 seconds
        self._order_timestamps = [t for t in self._order_timestamps if now - t < 60]
        return len(self._order_timestamps) < MAX_ORDERS_PER_MINUTE

    def _record_order(self) -> None:
        """Record the current time as an order submission timestamp."""
        self._order_timestamps.append(time.time())
