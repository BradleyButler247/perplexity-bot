"""
binance_indicators.py
---------------------
Real-time Binance market data streaming and technical indicator computation
for BTC and ETH, used by the crypto mean-reversion strategy.

Streams live trade data via Binance WebSocket and computes:
  1. OBI  — Order Book Imbalance (buy vs sell pressure)
  2. CVD  — Cumulative Volume Delta (aggressive buying vs selling)
  3. VWAP — Volume-Weighted Average Price
  4. RSI  — Relative Strength Index (14-period)
  5. MACD — Moving Average Convergence/Divergence (12/26/9)
  6. EMA  — Exponential Moving Average crossover (5/20)

All indicators are computed from real-time Binance data and exposed
as a simple dictionary for the crypto strategy to consume.

No API key required — uses public Binance WebSocket endpoints.

Architecture:
  - A background thread runs the WebSocket connection and accumulates
    trade data into rolling buffers.
  - The main bot thread calls get_signals("BTC") or get_signals("ETH")
    to get the latest indicator snapshot.
  - Indicators are recomputed on demand from the buffered data.
"""

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Deque, List, Optional

from http_client import get_session

logger = logging.getLogger("bot.binance")

# ── Configuration ────────────────────────────────────────────────────────────

# Binance WebSocket endpoints (no auth required)
BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"

# REST endpoint for order book snapshots
BINANCE_REST = "https://api.binance.com/api/v3"

# Symbols to track
SYMBOLS = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
}

# Rolling buffer sizes
MAX_TRADES = 500          # Keep last 500 trades (~5 min at normal activity)
MAX_CANDLES = 100         # Keep last 100 1-minute candles

# Indicator parameters
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
EMA_SHORT = 5
EMA_LONG = 20


@dataclass
class Trade:
    """A single trade from Binance."""
    price: float
    qty: float
    timestamp: float
    is_buyer_maker: bool   # True = seller aggressor (sell pressure)

    @property
    def is_buy(self) -> bool:
        """True if this was an aggressive buy (buyer took the ask)."""
        return not self.is_buyer_maker

    @property
    def usd_value(self) -> float:
        return self.price * self.qty


@dataclass
class CryptoSignals:
    """
    Snapshot of all indicators for a single asset (BTC or ETH).

    Consumed by the crypto_mean_reversion strategy.
    """
    symbol: str
    price: float = 0.0
    timestamp: float = 0.0

    # Order Book Imbalance: >0 = buy pressure, <0 = sell pressure, range [-1, 1]
    obi: float = 0.0

    # Cumulative Volume Delta: positive = net buying, negative = net selling
    cvd_1m: float = 0.0    # 1-minute CVD
    cvd_5m: float = 0.0    # 5-minute CVD

    # VWAP
    vwap: float = 0.0

    # RSI (0-100): <30 oversold, >70 overbought
    rsi: float = 50.0

    # MACD
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_divergence: bool = False  # True if MACD diverges from price

    # EMA crossover
    ema_short: float = 0.0    # EMA(5)
    ema_long: float = 0.0     # EMA(20)
    ema_bullish: bool = False  # True if EMA(5) > EMA(20)

    # Composite signal: -1.0 (strong bearish) to +1.0 (strong bullish)
    trend_score: float = 0.0
    trend_label: str = "NEUTRAL"  # "BULLISH", "BEARISH", "NEUTRAL"

    def __str__(self) -> str:
        return (
            f"CryptoSignals({self.symbol} ${self.price:.0f} | "
            f"trend={self.trend_label} score={self.trend_score:+.2f} | "
            f"RSI={self.rsi:.0f} OBI={self.obi:+.2f} "
            f"CVD5m={self.cvd_5m:+.0f})"
        )


class BinanceIndicators:
    """
    Streams Binance trade data and computes real-time technical indicators.

    Usage:
        bi = BinanceIndicators()
        bi.start()  # Start background WebSocket thread

        # Later, in the strategy:
        signals = bi.get_signals("BTC")
        if signals.trend_label == "BULLISH":
            ...

        bi.stop()
    """

    def __init__(self) -> None:
        self._trades: Dict[str, Deque[Trade]] = {
            sym: deque(maxlen=MAX_TRADES) for sym in SYMBOLS
        }
        self._prices: Dict[str, Deque[float]] = {
            sym: deque(maxlen=MAX_CANDLES) for sym in SYMBOLS
        }
        self._volumes: Dict[str, Deque[float]] = {
            sym: deque(maxlen=MAX_CANDLES) for sym in SYMBOLS
        }
        self._session = get_session()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_price: Dict[str, float] = {}
        self._last_orderbook: Dict[str, dict] = {}
        self._orderbook_ts: Dict[str, float] = {}

    def start(self) -> None:
        """Start the background WebSocket thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_ws, daemon=True, name="binance-ws"
        )
        self._thread.start()
        logger.info("Binance indicator stream started.")

    def stop(self) -> None:
        """Stop the background thread."""
        self._running = False
        logger.info("Binance indicator stream stopped.")

    def get_signals(self, symbol: str) -> CryptoSignals:
        """
        Compute and return the latest indicator snapshot for a symbol.

        Args:
            symbol: "BTC" or "ETH"

        Returns:
            CryptoSignals with all indicators populated.
        """
        signals = CryptoSignals(symbol=symbol, timestamp=time.time())

        trades = list(self._trades.get(symbol, []))
        if not trades:
            # Fall back to REST price if no WebSocket data yet
            self._fetch_price_rest(symbol, signals)
            return signals

        signals.price = trades[-1].price
        self._last_price[symbol] = signals.price

        # Compute indicators
        self._compute_obi(symbol, signals)
        self._compute_cvd(trades, signals)
        self._compute_vwap(trades, signals)
        self._compute_rsi(symbol, signals)
        self._compute_macd(symbol, signals)
        self._compute_ema(symbol, signals)
        self._compute_trend_score(signals)

        return signals

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket streaming
    # ─────────────────────────────────────────────────────────────────────────

    def _run_ws(self) -> None:
        """Run the WebSocket connection in a background thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_connect())
        except Exception as exc:
            logger.error("Binance WS error: %s", exc)
        finally:
            loop.close()

    async def _ws_connect(self) -> None:
        """Connect to Binance combined stream for all symbols."""
        try:
            import websockets
        except ImportError:
            logger.warning("websockets not installed — using REST fallback for Binance data.")
            self._run_rest_fallback()
            return

        streams = "/".join(f"{sym}@trade" for sym in SYMBOLS.values())
        url = f"{BINANCE_WS_BASE}/{streams}"

        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    logger.info("Binance WebSocket connected: %s", url)
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._handle_trade(json.loads(msg))
                        except asyncio.TimeoutError:
                            # Send ping to keep alive
                            await ws.ping()
                        except Exception as exc:
                            logger.debug("WS message error: %s", exc)
                            break
            except Exception as exc:
                logger.warning("Binance WS disconnected: %s — reconnecting in 5s", exc)
                await asyncio.sleep(5)

    def _handle_trade(self, data: dict) -> None:
        """Process a single trade event from the WebSocket."""
        symbol_raw = (data.get("s") or "").upper()

        # Map back to our symbol keys
        symbol = None
        for key, binance_sym in SYMBOLS.items():
            if binance_sym.upper() == symbol_raw:
                symbol = key
                break
        if not symbol:
            return

        trade = Trade(
            price=float(data.get("p", 0)),
            qty=float(data.get("q", 0)),
            timestamp=float(data.get("T", 0)) / 1000.0,
            is_buyer_maker=data.get("m", False),
        )

        self._trades[symbol].append(trade)

        # Update minute candle data
        self._prices[symbol].append(trade.price)
        self._volumes[symbol].append(trade.qty)

    def _run_rest_fallback(self) -> None:
        """Fallback: poll REST API if WebSocket isn't available."""
        while self._running:
            for symbol, binance_sym in SYMBOLS.items():
                try:
                    # Fetch recent trades
                    resp = self._session.get(
                        f"{BINANCE_REST}/trades",
                        params={"symbol": binance_sym.upper(), "limit": 50},
                        timeout=5,
                    )
                    if resp.ok:
                        for t in resp.json():
                            trade = Trade(
                                price=float(t["price"]),
                                qty=float(t["qty"]),
                                timestamp=float(t["time"]) / 1000.0,
                                is_buyer_maker=t["isBuyerMaker"],
                            )
                            self._trades[symbol].append(trade)
                            self._prices[symbol].append(trade.price)
                            self._volumes[symbol].append(trade.qty)
                except Exception:
                    pass
            time.sleep(3)

    # ─────────────────────────────────────────────────────────────────────────
    # Indicator computations
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_obi(self, symbol: str, signals: CryptoSignals) -> None:
        """
        Order Book Imbalance: ratio of bid vs ask volume near the mid-price.
        OBI > 0 = more buy pressure, OBI < 0 = more sell pressure.
        """
        now = time.time()
        binance_sym = SYMBOLS.get(symbol, "")

        # Refresh orderbook every 5 seconds
        if now - self._orderbook_ts.get(symbol, 0) < 5:
            book = self._last_orderbook.get(symbol, {})
        else:
            try:
                resp = self._session.get(
                    f"{BINANCE_REST}/depth",
                    params={"symbol": binance_sym.upper(), "limit": 20},
                    timeout=5,
                )
                book = resp.json() if resp.ok else {}
                self._last_orderbook[symbol] = book
                self._orderbook_ts[symbol] = now
            except Exception:
                book = {}

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        bid_volume = sum(float(b[1]) for b in bids[:10])
        ask_volume = sum(float(a[1]) for a in asks[:10])
        total = bid_volume + ask_volume

        if total > 0:
            signals.obi = (bid_volume - ask_volume) / total
        else:
            signals.obi = 0.0

    def _compute_cvd(self, trades: List[Trade], signals: CryptoSignals) -> None:
        """
        Cumulative Volume Delta: net aggressive buying minus selling.
        Positive = buyers dominating, negative = sellers dominating.
        """
        now = time.time()

        cvd_1m = 0.0
        cvd_5m = 0.0

        for t in trades:
            age = now - t.timestamp
            delta = t.usd_value if t.is_buy else -t.usd_value

            if age <= 60:
                cvd_1m += delta
            if age <= 300:
                cvd_5m += delta

        signals.cvd_1m = cvd_1m
        signals.cvd_5m = cvd_5m

    def _compute_vwap(self, trades: List[Trade], signals: CryptoSignals) -> None:
        """Volume-Weighted Average Price from recent trades."""
        total_value = sum(t.price * t.qty for t in trades)
        total_volume = sum(t.qty for t in trades)

        if total_volume > 0:
            signals.vwap = total_value / total_volume
        else:
            signals.vwap = signals.price

    def _compute_rsi(self, symbol: str, signals: CryptoSignals) -> None:
        """RSI(14) from recent price changes."""
        prices = list(self._prices.get(symbol, []))
        if len(prices) < RSI_PERIOD + 1:
            signals.rsi = 50.0
            return

        # Use the last RSI_PERIOD+1 prices
        recent = prices[-(RSI_PERIOD + 1):]
        gains = []
        losses = []

        for i in range(1, len(recent)):
            change = recent[i] - recent[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        if avg_loss == 0:
            signals.rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            signals.rsi = 100.0 - (100.0 / (1.0 + rs))

    def _compute_macd(self, symbol: str, signals: CryptoSignals) -> None:
        """MACD(12, 26, 9) from price history."""
        prices = list(self._prices.get(symbol, []))
        if len(prices) < MACD_SLOW + MACD_SIGNAL:
            return

        # Compute EMAs
        ema_fast = self._ema(prices, MACD_FAST)
        ema_slow = self._ema(prices, MACD_SLOW)

        if not ema_fast or not ema_slow:
            return

        # MACD line = EMA(fast) - EMA(slow)
        macd_values = [f - s for f, s in zip(ema_fast[-MACD_SIGNAL*2:], ema_slow[-MACD_SIGNAL*2:])]

        if len(macd_values) < MACD_SIGNAL:
            return

        # Signal line = EMA of MACD
        signal_values = self._ema(macd_values, MACD_SIGNAL)

        if signal_values:
            signals.macd_line = macd_values[-1]
            signals.macd_signal = signal_values[-1]
            signals.macd_histogram = signals.macd_line - signals.macd_signal

            # Simple divergence detection: price trending up but MACD trending down
            if len(prices) >= 10 and len(macd_values) >= 5:
                price_trend = prices[-1] - prices[-10]
                macd_trend = macd_values[-1] - macd_values[-5]
                if (price_trend > 0 and macd_trend < 0) or (price_trend < 0 and macd_trend > 0):
                    signals.macd_divergence = True

    def _compute_ema(self, symbol: str, signals: CryptoSignals) -> None:
        """EMA(5) and EMA(20) crossover detection."""
        prices = list(self._prices.get(symbol, []))
        if len(prices) < EMA_LONG:
            return

        ema_short = self._ema(prices, EMA_SHORT)
        ema_long = self._ema(prices, EMA_LONG)

        if ema_short and ema_long:
            signals.ema_short = ema_short[-1]
            signals.ema_long = ema_long[-1]
            signals.ema_bullish = signals.ema_short > signals.ema_long

    def _compute_trend_score(self, signals: CryptoSignals) -> None:
        """
        Composite trend score from all indicators.
        Range: -1.0 (strong bearish) to +1.0 (strong bullish).
        """
        score = 0.0

        # OBI: ±0.20
        score += signals.obi * 0.20

        # CVD 5m: ±0.20 (normalized)
        if signals.cvd_5m > 10000:
            score += 0.20
        elif signals.cvd_5m < -10000:
            score -= 0.20
        else:
            score += (signals.cvd_5m / 50000) * 0.20

        # RSI: ±0.20
        if signals.rsi > 70:
            score -= 0.20  # Overbought = bearish signal
        elif signals.rsi < 30:
            score += 0.20  # Oversold = bullish signal
        else:
            score += ((signals.rsi - 50) / 50) * 0.10

        # MACD histogram: ±0.15
        if signals.macd_histogram > 0:
            score += 0.15
        elif signals.macd_histogram < 0:
            score -= 0.15

        # MACD divergence: ±0.10 (counter-trend signal)
        if signals.macd_divergence:
            if signals.macd_histogram > 0:
                score -= 0.10  # Bearish divergence
            else:
                score += 0.10  # Bullish divergence

        # EMA crossover: ±0.15
        if signals.ema_bullish:
            score += 0.15
        elif signals.ema_short > 0 and signals.ema_long > 0:
            score -= 0.15

        # Clamp
        signals.trend_score = max(-1.0, min(1.0, score))

        # Label
        if signals.trend_score >= 0.30:
            signals.trend_label = "BULLISH"
        elif signals.trend_score <= -0.30:
            signals.trend_label = "BEARISH"
        else:
            signals.trend_label = "NEUTRAL"

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _ema(data: List[float], period: int) -> List[float]:
        """Compute Exponential Moving Average."""
        if len(data) < period:
            return []

        multiplier = 2.0 / (period + 1)
        ema_values = [sum(data[:period]) / period]

        for price in data[period:]:
            ema_values.append(
                (price - ema_values[-1]) * multiplier + ema_values[-1]
            )

        return ema_values

    def _fetch_price_rest(self, symbol: str, signals: CryptoSignals) -> None:
        """Fallback: get price from REST API."""
        binance_sym = SYMBOLS.get(symbol, "")
        if not binance_sym:
            return
        try:
            resp = self._session.get(
                f"{BINANCE_REST}/ticker/price",
                params={"symbol": binance_sym.upper()},
                timeout=5,
            )
            if resp.ok:
                signals.price = float(resp.json().get("price", 0))
                self._last_price[symbol] = signals.price
        except Exception:
            signals.price = self._last_price.get(symbol, 0)
