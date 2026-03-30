"""
strategies/crypto_mean_reversion.py
------------------------------------
Mean-reversion strategy targeting Polymarket's ultra-short-term crypto
Up/Down prediction markets (5-minute, 15-minute BTC and ETH windows).

Inspired by the Hcrystallash approach: buy outcome tokens at 35-68¢ when
mean-reversion conditions are met, hold to resolution at $1.00.

Key principles:
  1. ONLY targets crypto Up/Down markets (BTC and ETH).
  2. Uses external crypto price data (Chainlink / Binance) to determine
     real-time directional bias.
  3. Implements mean-reversion logic: buy when the market overreacts to
     short-term noise (price compression + reversal signals).
  4. Accumulates positions through multiple small buys rather than
     single large orders.
  5. Lets positions resolve naturally (no early exit — hold to $1.00 or $0).

Market structure:
  - Each 5-minute window is a separate market with "Up" and "Down" outcomes.
  - If BTC/ETH price at window end >= price at window start → "Up" resolves $1.
  - Otherwise → "Down" resolves $1.
  - Markets are found via Gamma API with series like "btc-up-or-down-5m".

Entry logic (adapted from tweet analysis):
  - Wait for price compression (narrow bid/ask spread on the outcome token).
  - Look for oversold/overbought conditions in the outcome token price.
  - Buy the contrarian side when the market has overreacted.
  - Target entry prices of 35-68¢ for maximum payoff at resolution.

This strategy does NOT sell before resolution — it holds to the binary
outcome ($1 or $0). Risk management is handled by position sizing.
"""

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from http_client import get_session
from strategies.base import BaseStrategy, TradeSignal
from market_scanner import MarketInfo, TokenInfo

try:
    from binance_indicators import BinanceIndicators, CryptoSignals
    HAS_BINANCE = True
except ImportError:
    HAS_BINANCE = False

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

# Market identification patterns
CRYPTO_MARKET_PATTERNS = [
    r"bitcoin\s+up\s+or\s+down",
    r"btc\s+up\s+or\s+down",
    r"ethereum\s+up\s+or\s+down",
    r"eth\s+up\s+or\s+down",
    r"bitcoin.*price",
    r"btc.*price",
    r"ethereum.*price",
    r"eth.*price",
    r"bitcoin.*above",
    r"bitcoin.*below",
    r"btc.*above",
    r"btc.*below",
    r"crypto.*up.*down",
]

# Series identifiers for crypto Up/Down markets
CRYPTO_SERIES_PATTERNS = [
    "btc-up", "eth-up", "btc-updown", "eth-updown",
    "bitcoin-up", "ethereum-up",
]

# Entry price range: only buy tokens priced between these values
# Hcrystallash buys at 35-68¢ — we use a slightly wider range
MIN_ENTRY_PRICE = 0.30   # Don't buy below 30¢ (too risky, too far from resolution)
MAX_ENTRY_PRICE = 0.70   # Don't buy above 70¢ (not enough upside)

# Sweet spot: prefer tokens in this range (best risk/reward)
SWEET_SPOT_LOW = 0.40
SWEET_SPOT_HIGH = 0.60

# Mean reversion: buy when token is below this relative to its recent average
MEAN_REVERSION_THRESHOLD = 0.92  # Buy when price is 92% or less of recent avg

# Minimum spread to avoid buying into tight/efficient markets
MIN_SPREAD = 0.02  # 2¢ minimum spread

# Maximum number of signals per cycle for this strategy
MAX_SIGNALS_PER_CYCLE = 2

# Crypto price API for real-time BTC/ETH prices
BINANCE_API = "https://api.binance.com/api/v3/ticker/price"

# Price history for mean reversion calculation
# token_id -> list of (timestamp, mid_price)
_price_history: Dict[str, List[Tuple[float, float]]] = {}

# Cooldown: don't re-enter same market within this window (seconds)
MARKET_COOLDOWN = 300  # 5 minutes


class CryptoMeanReversionStrategy(BaseStrategy):
    """
    Mean-reversion strategy for crypto Up/Down prediction markets.

    Targets 5-minute and 15-minute BTC/ETH binary markets on Polymarket.
    Buys outcome tokens at 35-68¢ when mean-reversion conditions are met,
    then holds to resolution at $1.00 (or loses at $0).
    """

    def name(self) -> str:
        return "crypto_mean_reversion"

    def __init__(self, *args, binance_indicators=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._session = get_session()
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}
        self._market_cooldown: Dict[str, float] = {}
        self._btc_price: Optional[float] = None
        self._eth_price: Optional[float] = None
        self._last_crypto_fetch: float = 0

        # Binance real-time indicators
        self._binance = binance_indicators
        if self._binance is None and HAS_BINANCE:
            try:
                self._binance = BinanceIndicators()
                self._binance.start()
                self.log.info("Binance indicators started for crypto strategy.")
            except Exception as exc:
                self.log.warning("Binance indicators unavailable: %s", exc)
                self._binance = None

    def scan(self) -> List[TradeSignal]:
        """
        Scan for mean-reversion opportunities in crypto Up/Down markets.
        """
        signals: List[TradeSignal] = []
        markets = self.market_scanner.get_markets()

        # Refresh crypto prices (rate-limited to once per 10 seconds)
        self._refresh_crypto_prices()

        # Filter to only crypto Up/Down markets
        crypto_markets = [m for m in markets if self._is_crypto_market(m)]

        if not crypto_markets:
            self.log.debug("No active crypto Up/Down markets found.")
            return []

        self.log.debug("Found %d crypto Up/Down market(s) to evaluate.", len(crypto_markets))

        for market in crypto_markets:
            try:
                market_signals = self._evaluate_market(market)
                signals.extend(market_signals)
                if len(signals) >= MAX_SIGNALS_PER_CYCLE:
                    break
            except Exception as exc:
                self.log.debug(
                    "Error evaluating crypto market %s: %s",
                    market.market_id[:16], exc,
                )

        if signals:
            self.log.info(
                "Crypto mean-reversion: %d signal(s) from %d market(s).",
                len(signals), len(crypto_markets),
            )

        return signals[:MAX_SIGNALS_PER_CYCLE]

    # ─────────────────────────────────────────────────────────────────────────
    # Market identification
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_crypto_market(market: MarketInfo) -> bool:
        """Check if a market is a crypto Up/Down binary market."""
        q = market.question.lower()

        # Check question text
        for pattern in CRYPTO_MARKET_PATTERNS:
            if re.search(pattern, q):
                return True

        # Check for Up/Down outcomes (not Yes/No)
        outcomes = [t.outcome.lower() for t in market.tokens]
        if "up" in outcomes and "down" in outcomes:
            # Additional check: must mention crypto
            if any(kw in q for kw in ["bitcoin", "btc", "ethereum", "eth", "crypto"]):
                return True

        # Check for Yes/No crypto markets (e.g., "Will BTC be above 90K?")
        if any(kw in q for kw in ["bitcoin", "btc", "ethereum", "eth", "crypto"]):
            yes_no_outcomes = {o.lower() for o in outcomes}
            if yes_no_outcomes & {"yes", "no"}:
                # Get the best ask price of any token
                token_prices = [t.best_ask for t in market.tokens if t.best_ask > 0]
                if token_prices and min(token_prices) < 0.70:
                    return True

        return False

    def _get_market_asset(self, market: MarketInfo) -> str:
        """Determine if this is a BTC or ETH market."""
        q = market.question.lower()
        if "bitcoin" in q or "btc" in q:
            return "BTC"
        if "ethereum" in q or "eth" in q:
            return "ETH"
        return "UNKNOWN"

    # ─────────────────────────────────────────────────────────────────────────
    # Market evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_market(self, market: MarketInfo) -> List[TradeSignal]:
        """
        Evaluate a single crypto Up/Down market for mean-reversion entry.

        Returns signals for tokens that meet all entry criteria.
        """
        signals = []

        # Check cooldown
        last_entry = self._market_cooldown.get(market.market_id, 0)
        if time.time() - last_entry < MARKET_COOLDOWN:
            return []

        asset = self._get_market_asset(market)

        for token in market.tokens:
            signal = self._evaluate_token(token, market, asset)
            if signal:
                signals.append(signal)
                self._market_cooldown[market.market_id] = time.time()

        return signals

    def _evaluate_token(
        self, token: TokenInfo, market: MarketInfo, asset: str
    ) -> Optional[TradeSignal]:
        """
        Check if a single outcome token (Up or Down) is a good mean-reversion buy.
        """
        price = token.mid_price or token.best_ask
        if price <= 0:
            return None

        # ── Price range filter ──────────────────────────────────────────────
        if price < MIN_ENTRY_PRICE or price > MAX_ENTRY_PRICE:
            return None

        # ── Spread check ────────────────────────────────────────────────────
        spread = token.best_ask - token.best_bid if token.best_ask > 0 and token.best_bid > 0 else 0
        if spread < MIN_SPREAD:
            # Market is too tight/efficient — no edge
            return None

        # ── Update price history ────────────────────────────────────────────
        self._update_price_history(token)

        # ── Mean reversion check (sigma-based) ─────────────────────────────
        avg_price, std_price = self._get_price_stats(token.token_id)
        if avg_price <= 0 or std_price <= 0:
            # Not enough history yet
            return None

        # Calculate z-score: how many standard deviations below the mean
        z_score = (price - avg_price) / std_price

        # Only buy when price is significantly below average (z < -2.0)
        if z_score > MEAN_REVERSION_SIGMA:
            return None

        # ── Compute confidence score ────────────────────────────────────────
        # Combines polymarket price analysis with Binance order flow indicators
        confidence = 0.0

        # Price position score (best at 50¢, where risk/reward is balanced)
        if SWEET_SPOT_LOW <= price <= SWEET_SPOT_HIGH:
            confidence += 0.25
        elif MIN_ENTRY_PRICE <= price < SWEET_SPOT_LOW:
            confidence += 0.15
        else:
            confidence += 0.10

        # Mean reversion depth (how far below average)
        reversion_depth = abs(z_score) / 4.0  # Normalise: -4σ = max depth
        confidence += min(reversion_depth * 0.30, 0.20)

        # Volume score
        if market.volume > 10000:
            confidence += 0.10
        elif market.volume > 1000:
            confidence += 0.05

        # ── Binance indicator boost ────────────────────────────────────────
        # Use real-time order flow to confirm or reject the mean reversion
        binance_boost = 0.0
        binance_info = ""
        if self._binance:
            try:
                signals = self._binance.get_signals(asset)
                binance_info = f"trend={signals.trend_label} RSI={signals.rsi:.0f} OBI={signals.obi:+.2f}"

                # Check if Binance confirms our direction
                is_buying_up = token.outcome.lower() == "up"
                is_buying_down = token.outcome.lower() == "down"

                if is_buying_up and signals.trend_label == "BULLISH":
                    binance_boost += 0.20  # Binance confirms upward move
                elif is_buying_down and signals.trend_label == "BEARISH":
                    binance_boost += 0.20  # Binance confirms downward move
                elif is_buying_up and signals.trend_label == "BEARISH":
                    binance_boost -= 0.15  # Binance contradicts — reduce confidence
                elif is_buying_down and signals.trend_label == "BULLISH":
                    binance_boost -= 0.15  # Binance contradicts

                # RSI confirmation
                if is_buying_up and signals.rsi < 30:
                    binance_boost += 0.10  # Oversold = bounce likely
                elif is_buying_down and signals.rsi > 70:
                    binance_boost += 0.10  # Overbought = drop likely

                # OBI confirmation (order book pressure)
                if is_buying_up and signals.obi > 0.3:
                    binance_boost += 0.05  # Strong buy pressure
                elif is_buying_down and signals.obi < -0.3:
                    binance_boost += 0.05  # Strong sell pressure

                # MACD divergence (strong mean reversion signal)
                if signals.macd_divergence:
                    binance_boost += 0.10

            except Exception as exc:
                self.log.debug("Binance indicator error: %s", exc)

        confidence += binance_boost

        # Spread score (wider spread = more potential edge)
        if spread > 0.05:
            confidence += 0.05

        confidence = max(0.0, min(confidence, 1.0))

        # ── Minimum confidence gate ─────────────────────────────────────────
        if confidence < 0.30:
            return None

        # ── Build signal ────────────────────────────────────────────────────
        # Size: in micro mode this will be overridden, but provide a reasonable default
        budget = self.cfg.MAX_POSITION_SIZE * confidence * 0.5
        size = budget / token.best_ask if token.best_ask > 0 else 0
        size = max(round(size, 2), 5.0)  # Minimum 5 shares for Polymarket

        # Expected payoff: buy at current price, resolve at $1.00
        expected_payoff = (1.0 - price) / price  # e.g., buy at 50¢ → 100% return

        bi_str = f"{binance_info} | " if binance_info else ""
        reason = (
            f"Crypto MR [{asset}] {token.outcome} @ {price:.3f} | "
            f"avg={avg_price:.3f} | z={z_score:.2f}σ | "
            f"spread={spread:.3f} | payoff={expected_payoff:.0%} | "
            f"{bi_str}"
            f"{market.question[:50]}"
        )

        signal = TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=token.token_id,
            side="BUY",
            price=round(token.best_ask, 4),  # Buy at the ask
            size=size,
            confidence=confidence,
            reason=reason,
            order_type="GTC",
        )

        self._log_signal(signal)
        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # Price tracking
    # ─────────────────────────────────────────────────────────────────────────

    def _update_price_history(self, token: TokenInfo) -> None:
        """Track token prices for mean reversion calculation."""
        price = token.mid_price or token.best_ask
        if price <= 0:
            return

        history = self._price_history.setdefault(token.token_id, [])
        history.append((time.time(), price))

        # Keep last 20 observations (~10 minutes at 30s intervals)
        if len(history) > 20:
            self._price_history[token.token_id] = history[-20:]

    def _get_average_price(self, token_id: str) -> float:
        """Get the simple moving average of recent prices for a token."""
        history = self._price_history.get(token_id, [])
        if len(history) < 3:
            return 0.0  # Need at least 3 observations

        prices = [p for _, p in history]
        return sum(prices) / len(prices)

    def _get_price_stats(self, token_id: str) -> tuple:
        """Get mean and standard deviation of recent prices."""
        import statistics
        history = self._price_history.get(token_id, [])
        if len(history) < 5:
            return 0.0, 0.0  # Need at least 5 observations for std dev

        prices = [p for _, p in history]
        avg = sum(prices) / len(prices)
        std = statistics.stdev(prices) if len(prices) >= 2 else 0.0
        return avg, std

    # ─────────────────────────────────────────────────────────────────────────
    # External crypto price data
    # ─────────────────────────────────────────────────────────────────────────

    def _refresh_crypto_prices(self) -> None:
        """Fetch current BTC and ETH prices from Binance (rate-limited)."""
        if time.time() - self._last_crypto_fetch < 10:
            return

        try:
            # BTC
            resp = self._session.get(
                BINANCE_API, params={"symbol": "BTCUSDT"}, timeout=5
            )
            if resp.ok:
                self._btc_price = float(resp.json().get("price", 0))

            # ETH
            resp = self._session.get(
                BINANCE_API, params={"symbol": "ETHUSDT"}, timeout=5
            )
            if resp.ok:
                self._eth_price = float(resp.json().get("price", 0))

            self._last_crypto_fetch = time.time()
            self.log.debug(
                "Crypto prices: BTC=$%.0f ETH=$%.0f",
                self._btc_price or 0, self._eth_price or 0,
            )
        except Exception as exc:
            self.log.debug("Failed to fetch crypto prices: %s", exc)
