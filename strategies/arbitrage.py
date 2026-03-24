"""
strategies/arbitrage.py
-----------------------
Sum-to-one arbitrage strategy.

Polymarket binary markets resolve to exactly $1.00 per winning share.
This means that for any market, the combined price of YES + NO must always
equal $1.00 at resolution.  When the combined *ask* prices of YES and NO fall
below $1.00 (minus fees), there is a risk-free arbitrage opportunity: buy both
sides, guarantee $1.00 at resolution, and net the difference.

Polymarket charges a 2% fee on winnings (i.e., you receive $0.98 per winning
dollar). The fee applies to the profit, not the purchase price.

Strategy logic:
  1. For each market: compute combined_ask = best_ask(YES) + best_ask(NO).
  2. If combined_ask < 1.0 - FEE - ARBITRAGE_MIN_EDGE → arbitrage exists.
  3. Size the trade to the available order-book liquidity on both sides.
  4. Generate two FOK (fill-or-kill) TradeSignal objects: one for YES, one for NO.

Fee calculation example:
  combined_ask = 0.94
  Spend $0.94, get $1.00 at resolution BUT pay 2% fee on $0.06 profit.
  Net payout = $1.00 - (0.02 × $1.00) = $0.98
  Actually: net = 1.00 - 0.02 = $0.98; spent $0.94 → profit = $0.04
  More precisely: effective_payout = 1.0 - fee_rate (fee is on winnings)
  Edge = effective_payout - combined_ask = 0.98 - 0.94 = 0.04 (4%)
"""

import logging
from typing import List

from strategies.base import BaseStrategy, TradeSignal
from market_scanner import MarketInfo

logger = logging.getLogger(__name__)

# Polymarket fee on winnings (2% = 0.02)
POLYMARKET_FEE = 0.02

# Minimum order size in shares (avoid dust orders)
MIN_ORDER_SIZE = 1.0


class ArbitrageStrategy(BaseStrategy):
    """
    Detects and signals sum-to-one arbitrage opportunities.

    For each qualifying market, two TradeSignal objects are returned:
    one to BUY YES and one to BUY NO.  Both use FOK order type so that
    either both fill immediately or neither does.
    """

    def name(self) -> str:
        return "arbitrage"

    def scan(self) -> List[TradeSignal]:
        """
        Scan all monitored markets for arbitrage opportunities.

        Returns:
            List of TradeSignal pairs (YES + NO) for each identified arb.
        """
        signals: List[TradeSignal] = []
        markets = self.market_scanner.get_markets()
        opportunities_found = 0
        opportunities_executed = 0

        for market in markets:
            try:
                result = self._check_market(market)
                if result:
                    yes_signal, no_signal = result
                    opportunities_found += 1
                    signals.extend([yes_signal, no_signal])
                    opportunities_executed += 1
                    self._log_signal(yes_signal)
                    self._log_signal(no_signal)
            except Exception as exc:
                logger.debug(
                    "Arbitrage scan error for %s: %s",
                    market.market_id[:16],
                    exc,
                )

        if opportunities_found > 0:
            self.log.info(
                "Arbitrage scan complete: %d opportunities found, "
                "%d signalled.",
                opportunities_found,
                opportunities_executed,
            )
        else:
            self.log.debug("Arbitrage scan complete: no opportunities found.")

        return signals

    def _check_market(
        self, market: MarketInfo
    ) -> None | tuple[TradeSignal, TradeSignal]:
        """
        Evaluate a single market for a sum-to-one arbitrage.

        Returns a (YES_signal, NO_signal) tuple if an opportunity exists,
        or None otherwise.
        """
        yes = market.yes_token
        no = market.no_token

        if yes is None or no is None:
            return None

        yes_ask = yes.best_ask
        no_ask = no.best_ask

        # Guard against zero/missing prices
        if yes_ask <= 0 or no_ask <= 0 or yes_ask >= 1.0 or no_ask >= 1.0:
            return None

        combined_ask = yes_ask + no_ask

        # Effective payout after Polymarket fee
        # The fee is applied to total winnings ($1.00 per share), not just profit.
        effective_payout = 1.0 - POLYMARKET_FEE  # = 0.98

        edge = effective_payout - combined_ask

        if edge < self.cfg.ARBITRAGE_MIN_EDGE:
            logger.debug(
                "Arb edge %.4f below threshold %.4f for %s",
                edge,
                self.cfg.ARBITRAGE_MIN_EDGE,
                market.question[:60],
            )
            return None

        # Determine tradeable size (limited by available liquidity on both sides)
        yes_available = yes.ask_size
        no_available = no.ask_size
        # Also check available USDC budget
        max_usd_per_leg = self.cfg.MAX_POSITION_SIZE / 2.0

        # Shares we can buy on each leg within our budget
        yes_max_shares = min(yes_available, max_usd_per_leg / yes_ask if yes_ask > 0 else 0)
        no_max_shares = min(no_available, max_usd_per_leg / no_ask if no_ask > 0 else 0)

        # Match the two legs: buy equal numbers of shares on both sides
        trade_size = min(yes_max_shares, no_max_shares)
        if trade_size < MIN_ORDER_SIZE:
            logger.debug(
                "Arb trade size %.2f too small (min %.2f) for %s",
                trade_size,
                MIN_ORDER_SIZE,
                market.question[:60],
            )
            return None

        reason = (
            f"Sum-to-one arb: YES_ask={yes_ask:.3f} + NO_ask={no_ask:.3f} = "
            f"{combined_ask:.3f} < {effective_payout:.2f} | edge={edge:.4f}"
        )

        yes_signal = TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=yes.token_id,
            side="BUY",
            price=yes_ask,
            size=trade_size,
            confidence=min(edge / 0.10, 1.0),  # scale confidence by edge
            reason=reason,
            order_type="FOK",
        )

        no_signal = TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=no.token_id,
            side="BUY",
            price=no_ask,
            size=trade_size,
            confidence=min(edge / 0.10, 1.0),
            reason=reason,
            order_type="FOK",
        )

        self.log.info(
            "Arb opportunity: %s | combined_ask=%.4f | edge=%.4f | size=%.2f",
            market.question[:70],
            combined_ask,
            edge,
            trade_size,
        )

        return yes_signal, no_signal
