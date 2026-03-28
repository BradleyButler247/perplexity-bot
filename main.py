"""
main.py
-------
Entry point for the Polymarket multi-strategy trading bot.

This module:
  1. Parses command-line arguments.
  2. Loads configuration from .env.
  3. Initialises all bot components.
  4. Runs the main synchronous scan loop (with asyncio WebSocket in background).
  5. Handles graceful shutdown on CTRL+C or SIGTERM.

Usage:
    python main.py              # Use TRADING_MODE from .env (default: paper)
    python main.py --paper      # Force paper trade mode
    python main.py --micro      # Force micro trading ($1.50 real orders)
    python main.py --live       # Force live trading (override to full size)
    python main.py --no-ws      # Disable WebSocket (REST-only mode)
    python main.py --strategies arbitrage signal_based
"""

import argparse
import asyncio
import datetime
import logging
import signal
import sys
import time
import threading
from typing import List, Optional

# ── Project imports ───────────────────────────────────────────────────────────
from logger_setup import setup_logging
from config import load_config, Config
from client_manager import init_client, get_client
from market_scanner import MarketScanner
from websocket_manager import WebSocketManager
from execution import Executor
from risk_manager import RiskManager
from position_tracker import PositionTracker
from trade_manager import TradeManager
from trade_history import TradeHistory
from wallet_discovery import WalletDiscovery
from strategy_optimizer import StrategyOptimizer
from redeemer import Redeemer
from pnl_tracker import PnLTracker
from http_client import close_session
from strategies import (
    ArbitrageStrategy,
    CopyTradingStrategy,
    SignalBasedStrategy,
    CryptoMeanReversionStrategy,
    ContrarianExtremeStrategy,
    AIPoweredStrategy,
    SportsMomentumStrategy,
    CrossMarketArbStrategy,
    WeatherForecastArbStrategy,
    LPRewardsStrategy,
    BaseStrategy,
    TradeSignal,
)

logger = logging.getLogger("bot.main")

# ── Banner ─────────────────────────────────────────────────────────────────────
BANNER = """
╔══════════════════════════════════════════════════════╗
║     Polymarket Multi-Strategy Trading Bot v1.3       ║
║     Polygon Network (USDC)                           ║
╚══════════════════════════════════════════════════════╝
"""

# Mode-specific banners injected after the main banner
_MODE_BANNERS = {
    "paper": (
        "════════════════════════════════════════════════════════\n"
        "  PAPER MODE — trades are logged but NOT submitted.\n"
        "════════════════════════════════════════════════════════"
    ),
    "micro": (
        "════════════════════════════════════════════════════════\n"
        "  MICRO MODE — real orders placed at $MICRO_TRADE_SIZE per trade.\n"
        "  Use this to verify live connectivity with minimal risk.\n"
        "════════════════════════════════════════════════════════"
    ),
    "live": (
        "════════════════════════════════════════════════════════\n"
        "  LIVE MODE — full-size real orders WILL be placed.\n"
        "  Ensure risk limits are set appropriately.\n"
        "════════════════════════════════════════════════════════"
    ),
}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Polymarket multi-strategy trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                        # Use TRADING_MODE from .env
  python main.py --paper                # Force paper mode (no real orders)
  python main.py --micro                # Force micro mode ($1.50 real orders)
  python main.py --live                 # Force live trading
  python main.py --strategies arbitrage # Run only arbitrage strategy
  python main.py --no-ws --paper        # REST-only paper trade
        """,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--paper",
        action="store_true",
        help="Force paper trade mode (log trades but never submit)",
    )
    mode.add_argument(
        "--micro",
        action="store_true",
        help="Force micro mode (real orders at MICRO_TRADE_SIZE per trade)",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Force live trading (requires funded Polymarket account)",
    )

    parser.add_argument(
        "--strategies",
        nargs="+",
        choices=["arbitrage", "copy_trading", "signal_based", "crypto_mean_reversion", "contrarian_extreme", "ai_powered", "sports_momentum", "cross_market_arb", "weather_forecast_arb", "lp_rewards"],
        default=None,
        help="Specify which strategies to run (default: all)",
    )

    parser.add_argument(
        "--no-ws",
        action="store_true",
        help="Disable WebSocket connections (REST-only polling mode)",
    )

    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the .env file (default: .env)",
    )

    return parser.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket runner
# ─────────────────────────────────────────────────────────────────────────────

def run_websocket_in_thread(ws_manager: WebSocketManager) -> Optional[threading.Thread]:
    """
    Start the WebSocket manager in a background daemon thread with its own
    asyncio event loop.

    Returns:
        The started Thread, or None on failure.
    """
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(ws_manager.start())
            loop.run_forever()
        except Exception as exc:
            logger.error("WebSocket thread error: %s", exc, exc_info=True)
        finally:
            loop.close()

    thread = threading.Thread(target=_run, daemon=True, name="ws-thread")
    thread.start()
    logger.info("WebSocket background thread started.")
    return thread


# ─────────────────────────────────────────────────────────────────────────────
# Main bot class
# ─────────────────────────────────────────────────────────────────────────────

class TradingBot:
    """
    Orchestrates the bot lifecycle: initialise, run, shutdown.

    Components are initialised in order of dependency:
      Config → Logger → Client → Scanner → Executor → RiskManager
      → PositionTracker → TradeManager → TradeHistory
      → WalletDiscovery → StrategyOptimizer → Strategies → WebSocketManager
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cfg: Optional[Config] = None
        self.scanner: Optional[MarketScanner] = None
        self.executor: Optional[Executor] = None
        self.risk_manager: Optional[RiskManager] = None
        self.tracker: Optional[PositionTracker] = None
        self.trade_manager: Optional[TradeManager] = None
        self.trade_history: Optional[TradeHistory] = None
        self.wallet_discovery: Optional[WalletDiscovery] = None
        self.optimizer: Optional[StrategyOptimizer] = None
        self.redeemer: Optional[Redeemer] = None
        self.ws_manager: Optional[WebSocketManager] = None
        self.ai_engine = None  # AIProbabilityEngine — shared by AI strategy + trade manager
        self.strategies: List[BaseStrategy] = []
        self._shutdown = False
        self._ws_thread: Optional[threading.Thread] = None

    def initialise(self) -> None:
        """Set up all components.  Raises on fatal errors."""
        # ── Load config ────────────────────────────────────────────────────
        self.cfg = load_config(self.args.env)

        # Override trading mode from CLI flags (highest priority)
        if self.args.paper:
            self.cfg.TRADING_MODE = "paper"
            self.cfg.PAPER_TRADE = True
        elif self.args.micro:
            self.cfg.TRADING_MODE = "micro"
            self.cfg.PAPER_TRADE = False
        elif self.args.live:
            self.cfg.TRADING_MODE = "live"
            self.cfg.PAPER_TRADE = False

        # ── Logging ────────────────────────────────────────────────────────
        setup_logging(self.cfg.LOG_LEVEL)
        print(BANNER)
        print(_MODE_BANNERS.get(self.cfg.TRADING_MODE, ""))
        print()
        logger.info("Starting bot | %s", self.cfg.summary())

        # Mode-specific startup warnings
        mode = self.cfg.TRADING_MODE
        if mode == "paper":
            logger.warning("=" * 60)
            logger.warning("PAPER MODE — no real orders will be placed.")
            logger.warning("=" * 60)
        elif mode == "micro":
            logger.warning("=" * 60)
            logger.warning(
                "MICRO MODE — real orders at $%.2f each.",
                self.cfg.MICRO_TRADE_SIZE,
            )
            logger.warning("=" * 60)
        else:
            logger.warning("LIVE TRADING MODE — full-size real orders WILL be placed.")

        # ── CLOB client ─────────────────────────────────────────────────────
        client = init_client(self.cfg)

        # ── Market scanner ─────────────────────────────────────────────────
        self.scanner = MarketScanner(self.cfg, client)

        # ── Execution ──────────────────────────────────────────────────────
        self.executor = Executor(self.cfg, client)

        # ── Position tracker ───────────────────────────────────────────────
        self.tracker = PositionTracker(self.cfg, self.scanner)

        # Derive wallet address from client for position lookups
        try:
            address = client.get_address()
            self.tracker.set_wallet(address)
        except Exception as exc:
            logger.warning("Could not get wallet address from client: %s", exc)
            if self.cfg.POLYMARKET_PROXY_ADDRESS:
                self.tracker.set_wallet(self.cfg.POLYMARKET_PROXY_ADDRESS)

        self.tracker.load()

        # ── Sync positions from Polymarket ─────────────────────────────────
        self._sync_positions_from_chain()

        # ── Risk manager ────────────────────────────────────────────────────
        self.risk_manager = RiskManager(self.cfg, self.tracker)

        # ── AI Probability Engine (shared) ───────────────────────────────
        from ai_probability_engine import AIProbabilityEngine
        self.ai_engine = AIProbabilityEngine(self.cfg)

        # ── Trade manager ──────────────────────────────────────────────────
        self.trade_manager = TradeManager(
            tracker=self.tracker,
            executor=self.executor,
            cfg=self.cfg,
            market_scanner=self.scanner,
            ai_engine=self.ai_engine,
        )
        logger.info(
            "TradeManager ready | tp=%.0f%% sl=%.0f%% trail_act=%.0f%% trail_stop=%.0f%% max_hold=%dh",
            self.cfg.TAKE_PROFIT_PCT * 100,
            self.cfg.STOP_LOSS_PCT * 100,
            self.cfg.TRAILING_STOP_ACTIVATION * 100,
            self.cfg.TRAILING_STOP_PCT * 100,
            self.cfg.MAX_HOLD_TIME // 3600,
        )

        # ── Trade history ──────────────────────────────────────────────────
        self.trade_history = TradeHistory()
        logger.info("TradeHistory loaded from trade_history.csv.")

        # ── Auto-redeemer ─────────────────────────────────────────────────────
        try:
            self.redeemer = Redeemer(self.cfg)
            logger.info("Auto-redeemer ready.")
        except Exception as exc:
            logger.warning("Redeemer init failed: %s", exc)
            self.redeemer = None

        # ── Wallet discovery ───────────────────────────────────────────────
        if self.cfg.AUTO_DISCOVER_WALLETS:
            self.wallet_discovery = WalletDiscovery(self.cfg)
            logger.info(
                "WalletDiscovery enabled | interval=%dh | min_wr=%.0f%% | max_wallets=%d",
                self.cfg.WALLET_DISCOVERY_INTERVAL // 3600,
                self.cfg.MIN_WIN_RATE * 100,
                self.cfg.MAX_COPY_WALLETS,
            )
        else:
            logger.info("WalletDiscovery disabled (AUTO_DISCOVER_WALLETS=false).")

        # ── Strategy optimizer (self-learning) ─────────────────────────────
        self.optimizer = StrategyOptimizer(self.cfg, self.trade_history)
        logger.info(
            "StrategyOptimizer ready | min_trades=%d | interval=%ds | max_shift=%.0f%% | enabled=%s",
            self.optimizer.min_trades,
            self.optimizer.interval,
            self.optimizer.max_param_shift * 100,
            self.optimizer.enabled,
        )

        # Apply any previously-tuned parameters from optimizer state
        self._apply_tuned_params()


        # ── P&L tracker ──────────────────────────────────────────────────
        self.pnl_tracker = PnLTracker(self.tracker, self.trade_history)
        logger.info("P&L tracker ready. Reports saved to reports/ directory.")
        # ── Strategies ─────────────────────────────────────────────────────
        self._init_strategies()

        # ── WebSocket ──────────────────────────────────────────────────────
        if not self.args.no_ws:
            self._init_websocket()

        logger.info(
            "Bot initialised. %d strategies active. Optimizer: %s (regime=%s)",
            len(self.strategies),
            "ON" if self.optimizer.enabled else "OFF",
            self.optimizer.get_regime(),
        )

    def _init_strategies(self) -> None:
        """Instantiate enabled strategies."""
        enabled = self.args.strategies or ["arbitrage", "copy_trading", "signal_based", "crypto_mean_reversion", "contrarian_extreme", "ai_powered", "sports_momentum", "cross_market_arb", "weather_forecast_arb", "lp_rewards"]

        client = get_client()
        common_kwargs = dict(
            cfg=self.cfg,
            client=client,
            market_scanner=self.scanner,
            risk_manager=self.risk_manager,
            executor=self.executor,
        )

        for name in enabled:
            if name == "arbitrage":
                strategy = ArbitrageStrategy(**common_kwargs)
            elif name == "copy_trading":
                # Pass the WalletDiscovery instance so the strategy can
                # auto-discover wallets when TARGET_WALLET is not set.
                strategy = CopyTradingStrategy(
                    **common_kwargs,
                    wallet_discovery=self.wallet_discovery,
                )
            elif name == "signal_based":
                strategy = SignalBasedStrategy(**common_kwargs)
            elif name == "crypto_mean_reversion":
                strategy = CryptoMeanReversionStrategy(**common_kwargs)
            elif name == "contrarian_extreme":
                strategy = ContrarianExtremeStrategy(
                    **common_kwargs,
                    position_tracker=self.tracker,
                )
            elif name == "ai_powered":
                strategy = AIPoweredStrategy(**common_kwargs, ai_engine=self.ai_engine)
            elif name == "sports_momentum":
                strategy = SportsMomentumStrategy(**common_kwargs)
            elif name == "cross_market_arb":
                strategy = CrossMarketArbStrategy(**common_kwargs)
            elif name == "weather_forecast_arb":
                strategy = WeatherForecastArbStrategy(**common_kwargs)
            elif name == "lp_rewards":
                strategy = LPRewardsStrategy(**common_kwargs)
            else:
                logger.warning("Unknown strategy '%s'; skipping.", name)
                continue

            self.strategies.append(strategy)
            logger.info("Strategy loaded: %s", name)

    def _init_websocket(self) -> None:
        """Initialise the WebSocket manager and subscribe to tracked markets."""
        self.ws_manager = WebSocketManager(self.cfg)

        # Register a callback to update the scanner's price cache in real-time
        def on_best_bid_ask(event: dict) -> None:
            """Update in-memory token prices when WS delivers BBA updates."""
            asset_id = event.get("asset_id")
            best_ask = float(event.get("best_ask", 0))
            best_bid = float(event.get("best_bid", 0))
            # Update scanner cache
            for market in self.scanner._cache.values():
                for token in market.tokens:
                    if token.token_id == asset_id:
                        token.best_ask = best_ask
                        token.best_bid = best_bid
                        if best_bid > 0 and best_ask < 1:
                            token.mid_price = (best_bid + best_ask) / 2
                        return

        self.ws_manager.on_best_bid_ask(on_best_bid_ask)

        # Subscribe to all currently scanned markets
        markets = self.scanner.get_markets(force_refresh=True)
        token_ids = [t.token_id for m in markets for t in m.tokens]
        self.ws_manager.subscribe_market(token_ids)
        logger.info("WebSocket subscribed to %d tokens.", len(token_ids))

        self._ws_thread = run_websocket_in_thread(self.ws_manager)

    @staticmethod
    def _timestamp() -> str:
        """Return a compact HH:MM:SS timestamp for console output."""
        return datetime.datetime.now().strftime("%H:%M:%S")

    def _print_status(self, msg: str) -> None:
        """Print a timestamped status line to console (not logged to file)."""
        print(f"  [{self._timestamp()}] {msg}", flush=True)

    def run(self) -> None:
        """Main scan loop. Runs until shutdown is requested."""
        logger.info("Bot running. Press CTRL+C to stop.")
        print(f"\n  [{self._timestamp()}] Bot is live. Scanning every {self.cfg.POLL_INTERVAL}s.\n", flush=True)

        cycle = 0
        while not self._shutdown:
            cycle += 1
            start_ts = time.time()

            try:
                self._run_cycle(cycle)
            except Exception as exc:
                logger.error("Error in cycle %d: %s", cycle, exc, exc_info=True)

            elapsed = time.time() - start_ts
            sleep_time = max(0, self.cfg.POLL_INTERVAL - elapsed)
            logger.debug("Cycle %d complete in %.1fs. Sleeping %.1fs.", cycle, elapsed, sleep_time)

            # Interruptible sleep with countdown
            remaining = int(sleep_time)
            while remaining > 0 and not self._shutdown:
                # Show countdown every 5 seconds
                if remaining % 5 == 0 or remaining == int(sleep_time):
                    sys.stdout.write(
                        f"\r  [{self._timestamp()}] ⏳ Next scan in {remaining}s...   "
                    )
                    sys.stdout.flush()
                time.sleep(1)
                remaining -= 1

            # Clear the countdown line
            if not self._shutdown:
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()

    def _run_cycle(self, cycle: int) -> None:
        """Execute one complete scan-and-trade cycle."""
        cycle_start = time.time()
        logger.info("─── Cycle %d [%s] ───", cycle, self.cfg.TRADING_MODE.upper())
        print(f"\n  ┌─── Cycle {cycle} [{self.cfg.TRADING_MODE.upper()}] " + "─" * 35, flush=True)

        # ── 1. Update market data ──────────────────────────────────────────
        self._print_status("📡 Fetching markets...")
        try:
            markets = self.scanner.get_markets()
            n_markets = len(markets) if markets else 0
            n_tokens = sum(len(m.tokens) for m in markets) if markets else 0
            self._print_status(f"📡 Found {n_markets} markets ({n_tokens} tokens)")
        except Exception as exc:
            self._print_status(f"⚠️  Market scan failed: {exc}")
            logger.warning("Market scan failed: %s", exc)

        # ── 2. Update WebSocket subscriptions (for new markets) ────────────
        if self.ws_manager and cycle % 10 == 0:
            self._print_status("🔌 Refreshing WebSocket subscriptions...")
            markets = self.scanner.get_markets()
            token_ids = [t.token_id for m in markets for t in m.tokens]
            self.ws_manager.subscribe_market(token_ids)

        # ── 3. Run each strategy ───────────────────────────────────────────
        self._print_status("🔍 Running strategies...")
        all_signals: List[TradeSignal] = []
        for strategy in self.strategies:
            try:
                signals = strategy.scan()
                all_signals.extend(signals)
                status_icon = "✅" if signals else "·"
                self._print_status(
                    f"   {status_icon} {strategy.name()}: {len(signals)} signal(s)"
                )
                if signals:
                    logger.info(
                        "Strategy [%s] produced %d signal(s).",
                        strategy.name(),
                        len(signals),
                    )
            except Exception as exc:
                self._print_status(f"   ❌ {strategy.name()}: error")
                logger.error(
                    "Strategy [%s] error: %s", strategy.name(), exc, exc_info=True
                )

        logger.info("Total signals this cycle: %d", len(all_signals))

        # ── 4. Risk gate + optimizer gate + execute ────────────────────────
        executed = 0
        optimizer_filtered = 0
        for signal in all_signals:
            try:
                # Optimizer gate: probabilistically suppress signals from
                # underperforming strategies (always allows ≥10% through).
                # BYPASS: Don't filter until the optimizer has enough data
                # to make informed decisions (min_trades threshold).
                has_enough_data = (
                    self.trade_history
                    and len(self.trade_history.get_records()) >= self.optimizer.min_trades
                )
                if (
                    self.optimizer
                    and has_enough_data
                    and not self.optimizer.should_execute_signal(signal)
                ):
                    optimizer_filtered += 1
                    logger.debug(
                        "Signal filtered by optimizer: %s (weight=%.1f%%)",
                        signal.strategy,
                        self.optimizer.get_strategy_weight(signal.strategy) * 100,
                    )
                    continue

                # Smart entry filter: tie max price to time-to-resolution.
                # - High prices (90¢+) are fine if the market resolves soon
                #   (e.g., 5-minute crypto markets).
                # - High prices with distant resolution = risk without payoff,
                #   because the time exit will force-close before resolution.
                # - Markets resolving beyond 2x MAX_HOLD_TIME are skipped
                #   regardless of price.
                if signal.side == "BUY" and self.scanner:
                    market_info = self.scanner.get_market(signal.market_id)
                    if market_info and market_info.end_date:
                        try:
                            import datetime as _dt
                            end_str = market_info.end_date
                            if "T" in end_str:
                                end_dt = _dt.datetime.fromisoformat(
                                    end_str.replace("Z", "+00:00")
                                )
                            else:
                                end_dt = _dt.datetime.fromisoformat(end_str)
                            if end_dt.tzinfo is None:
                                end_dt = end_dt.replace(tzinfo=_dt.timezone.utc)
                            now_dt = _dt.datetime.now(_dt.timezone.utc)
                            hours_to_resolution = (end_dt - now_dt).total_seconds() / 3600
                            max_hold_hours = self.cfg.MAX_HOLD_TIME / 3600

                            # Tiered entry rules based on time to resolution:
                            #
                            # Resolves within hold time (≤24h):
                            #   Any price OK — we hold to resolution.
                            #
                            # Resolves 24-48h out:
                            #   Price must be ≤ 90¢ — need upside since
                            #   we might time-exit before resolution.
                            #
                            # Resolves beyond 48h:
                            #   Price must be ≤ 55¢ — only enter with
                            #   significant upside to compensate for
                            #   guaranteed early exit. These are swing
                            #   trades on volatile markets only.

                            if hours_to_resolution <= max_hold_hours:
                                pass  # Any price OK — hold to resolution

                            elif hours_to_resolution <= max_hold_hours * 2:
                                # 24-48h: allow up to and including 90¢
                                if signal.price > 0.90:
                                    logger.debug(
                                        "Signal skipped: price %.3f > $0.90 for "
                                        "%.0fh market (hold limit %.0fh). %s",
                                        signal.price, hours_to_resolution,
                                        max_hold_hours, signal.market_id[:16],
                                    )
                                    continue

                            else:
                                # Beyond 48h: only cheap entries with real
                                # upside (swing trades on volatile markets)
                                if signal.price > 0.55:
                                    logger.debug(
                                        "Signal skipped: price %.3f > $0.55 for "
                                        "long-dated market (%.0fh to resolution). %s",
                                        signal.price, hours_to_resolution,
                                        signal.market_id[:16],
                                    )
                                    continue
                        except Exception:
                            pass  # If we can't parse the date, allow the trade

                # Expected Value filter: only enter trades with positive EV.
                # EV = (prob_win * payout) - (prob_lose * cost)
                # For binary markets: payout = $1.00, cost = price
                ev = self._compute_ev(signal)
                if ev <= 0:
                    logger.debug(
                        "Signal skipped: negative EV=%.4f | %s @ $%.3f (conf=%.2f)",
                        ev, signal.strategy, signal.price, signal.confidence,
                    )
                    continue

                # In micro mode, relax position-size checks since each trade
                # is tiny (at most MICRO_TRADE_SIZE USD).
                if self.cfg.TRADING_MODE == "micro":
                    approved = self._approve_micro_trade(signal)
                else:
                    approved = self.risk_manager.approve_trade(signal)

                if approved:
                    # v34: Apply base-rate sizing before execution
                    if signal.side == "BUY" and self.trade_manager:
                        signal = self.trade_manager.apply_base_rate_sizing(signal)

                    # Cap trades per cycle to prevent runaway ordering
                    max_per_cycle = 3 if self.cfg.TRADING_MODE == "micro" else 5
                    if executed >= max_per_cycle:
                        logger.info(
                            "Trade cap reached (%d/%d this cycle); skipping remaining signals.",
                            executed, max_per_cycle,
                        )
                        break

                    result = self.executor.execute(signal)
                    if result.success:
                        executed += 1
                        # Record in position tracker (all modes)
                        self.tracker.record_trade(
                            token_id=signal.token_id,
                            market_id=signal.market_id,
                            outcome=signal.side,
                            side=signal.side,
                            size=result.filled_size or signal.size,
                            price=result.filled_price or signal.price,
                        )
                        # Update P&L tracker
                        usd_spent = (result.filled_price or signal.price) * (result.filled_size or signal.size)
                        self.risk_manager.update_pnl(-usd_spent)

                        # Record to trade history
                        if self.trade_history:
                            self.trade_history.record_trade(result)

            except Exception as exc:
                logger.error("Execution error for signal %s: %s", signal, exc, exc_info=True)

        if executed or optimizer_filtered:
            self._print_status(
                f"💰 Executed {executed} trade(s) ({optimizer_filtered} filtered by optimizer)"
            )
            logger.info(
                "Executed %d trade(s) this cycle (%d filtered by optimizer).",
                executed, optimizer_filtered,
            )
        elif all_signals:
            self._print_status("⛔ No trades passed risk/optimizer gates")
        else:
            self._print_status("💤 No trade signals this cycle")

        # ── 5. Manage open positions (take-profit, stop-loss, etc.) ────────
        n_positions = self.tracker.position_count()
        if self.trade_manager and n_positions > 0:
            self._print_status(f"📊 Managing {n_positions} open position(s)...")
            try:
                exit_results = self.trade_manager.manage_positions()
                n_exits = sum(1 for r in exit_results if r and r.success)
                for exit_result in exit_results:
                    if exit_result and exit_result.success:
                        if self.trade_history:
                            self.trade_history.record_trade(exit_result)
                if n_exits:
                    self._print_status(f"📊 Closed {n_exits} position(s)")
                    logger.info(
                        "TradeManager executed %d exit(s) this cycle.", n_exits,
                    )
            except Exception as exc:
                logger.warning("TradeManager error: %s", exc)
        elif n_positions > 0:
            self._print_status(f"📊 {n_positions} open position(s) — no exits triggered")

        # ── 6. Update positions ────────────────────────────────────────────
        try:
            self.tracker.refresh()
        except Exception as exc:
            logger.warning("Position tracker refresh failed: %s", exc)

        # ── 7. Auto-redeem resolved positions ────────────────────────────
        if self.redeemer and cycle % 10 == 0:
            try:
                n_redeemed = self.redeemer.redeem_all()
                if n_redeemed:
                    self._print_status(
                        f"\U0001f4b0 Redeemed {n_redeemed} resolved position(s)"
                    )
            except Exception as exc:
                logger.debug("Redeemer error: %s", exc)

        # ── 8. Run optimizer (self-learning) ──────────────────────────────
        if self.optimizer:
            try:
                optimized = self.optimizer.maybe_optimize()
                if optimized:
                    self._apply_tuned_params()
                    self._print_status(
                        f"🧠 Optimizer adapted | regime={self.optimizer.get_regime()}"
                    )
                    logger.info(
                        "Optimizer applied | regime=%s | weights=%s",
                        self.optimizer.get_regime(),
                        {k: f"{v:.0%}" for k, v in
                         self.optimizer.state.strategy_weights.items()},
                    )
            except Exception as exc:
                logger.warning("Optimizer error: %s", exc)

        # ── 8. Periodic status summary ─────────────────────────────────────
        logger.info(self.tracker.summary())
        logger.info(
            "Risk: kill_switch=%s daily_pnl=$%.2f",
            self.risk_manager.kill_switch_active,
            self.risk_manager.daily_pnl,
        )

        # ── 9. Show open orders ──────────────────────────────────────────────
        self._print_open_orders()


        # Update P&L tracker
        if hasattr(self, "pnl_tracker"):
            self.pnl_tracker.update()
        # Print cycle summary line
        cycle_elapsed = time.time() - cycle_start
        pnl_str = f"${self.risk_manager.daily_pnl:+.2f}"
        history_count = len(self.trade_history.get_records()) if self.trade_history else 0
        print(
            f"  └── Done in {cycle_elapsed:.1f}s | "
            f"positions: {self.tracker.position_count()} | "
            f"daily P&L: {pnl_str} | "
            f"total trades: {history_count}",
            flush=True,
        )

        if self.risk_manager.kill_switch_active:
            logger.critical("Kill switch is active! Halting trading loop.")
            self._shutdown = True

    def _approve_micro_trade(self, signal: TradeSignal) -> bool:
        """
        Relaxed risk gate for micro mode.

        Since micro positions are tiny (MICRO_TRADE_SIZE USD), the normal
        MAX_POSITION_SIZE check is bypassed; only the kill switch and total
        position count limits are enforced.

        Returns:
            True if the trade is approved.
        """
        if self.risk_manager.kill_switch_active:
            return False

        # Only enforce position count in micro mode
        n_positions = self.tracker.position_count()
        if n_positions >= self.cfg.MAX_POSITIONS:
            logger.info(
                "Trade rejected [micro]: position_count=%d >= MAX_POSITIONS=%d",
                n_positions,
                self.cfg.MAX_POSITIONS,
            )
            return False

        logger.debug(
            "Micro trade approved: %s | $%.2f (micro_size=$%.2f)",
            signal.strategy,
            signal.price * signal.size,
            self.cfg.MICRO_TRADE_SIZE,
        )
        return True

    @staticmethod
    def _compute_ev(signal) -> float:
        """
        Compute the Expected Value of a trade signal.

        For binary prediction markets:
            EV = (p_win * payout) - (p_lose * cost)

        where:
            p_win  = signal.confidence (our estimated probability of winning)
            p_lose = 1 - p_win
            payout = $1.00 - price (profit if we win)
            cost   = price (what we lose if we're wrong)

        A positive EV means the trade is worth taking in expectation.
        We use a small buffer (EV must exceed 1%) to filter marginal trades.
        """
        p_win = max(min(signal.confidence, 0.99), 0.01)
        p_lose = 1.0 - p_win
        payout = 1.0 - signal.price   # profit per share if correct
        cost = signal.price            # loss per share if wrong

        ev = (p_win * payout) - (p_lose * cost)
        return ev

    def _sync_positions_from_chain(self) -> None:
        """
        Fetch actual open positions from the Polymarket Data API and
        replace the local position tracker state with ground truth.

        This ensures the bot starts with an accurate view of positions
        regardless of what happened in previous sessions.
        """
        import requests as _requests

        # Determine the wallet address to query
        wallet = self.cfg.POLYMARKET_PROXY_ADDRESS
        if not wallet:
            try:
                wallet = get_client().get_address()
            except Exception:
                pass

        if not wallet:
            logger.warning("Cannot sync positions: no wallet address available.")
            return

        print(f"  [{self._timestamp()}] 🔄 Syncing positions from Polymarket...", flush=True)
        logger.info("Syncing positions from Data API for wallet %s...", wallet[:10])

        try:
            resp = _requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": wallet, "sizeThreshold": 0.01, "limit": 200},
                timeout=15,
            )
            resp.raise_for_status()
            positions_data = resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch positions from Data API: %s", exc)
            print(f"  [{self._timestamp()}] ⚠️  Could not sync positions: {exc}", flush=True)
            return

        if not positions_data:
            logger.info("Data API returned 0 open positions.")
            print(f"  [{self._timestamp()}] 🔄 No open positions on-chain", flush=True)
            # Clear local tracker to match
            self.tracker._positions.clear()
            self.tracker.save()
            return

        # Clear existing tracker and rebuild from chain data
        self.tracker._positions.clear()
        synced = 0

        for pos in positions_data:
            token_id = pos.get("asset", "")
            market_id = pos.get("conditionId", "")
            size = float(pos.get("size", 0) or 0)
            avg_price = float(pos.get("avgPrice", 0) or 0)
            cur_price = float(pos.get("curPrice", 0) or 0)
            outcome = pos.get("outcome", "")
            title = pos.get("title", "Unknown")
            cash_pnl = float(pos.get("cashPnl", 0) or 0)

            if size < 0.01 or not token_id:
                continue

            from position_tracker import Position
            self.tracker._positions[token_id] = Position(
                token_id=token_id,
                market_id=market_id,
                outcome=outcome,
                side="BUY",
                size=size,
                entry_price=avg_price,
                current_price=cur_price,
            )
            synced += 1

            usd_val = size * cur_price if cur_price > 0 else size * avg_price
            print(
                f"  [{self._timestamp()}] 🔄  {outcome} {size:.1f} shares @ ${avg_price:.3f} "
                f"(now ${cur_price:.3f}, P&L ${cash_pnl:+.2f}) | {title[:50]}",
                flush=True,
            )

        self.tracker.save()
        logger.info("Synced %d position(s) from Data API.", synced)
        print(
            f"  [{self._timestamp()}] ✅ Synced {synced} position(s) from Polymarket",
            flush=True,
        )

    def _print_open_orders(self) -> None:
        """
        Fetch and display all open (unfilled) limit orders from the CLOB.
        """
        try:
            client = get_client()
            orders = client.get_orders()

            if not orders:
                self._print_status("📋 No open limit orders")
                return

            # Filter to only live/active orders
            live_orders = [
                o for o in orders
                if o.get("status", "").lower() in ("live", "active", "open", "matched")
                or o.get("size_matched", "0") != o.get("original_size", "0")
            ]

            if not live_orders:
                self._print_status("📋 No open limit orders")
                return

            self._print_status(f"📋 {len(live_orders)} open limit order(s):")
            for order in live_orders:
                side = order.get("side", "?")
                price = order.get("price", "?")
                size_matched = float(order.get("size_matched", 0) or 0)
                original_size = float(order.get("original_size", 0) or order.get("size", 0) or 0)
                remaining = original_size - size_matched
                status = order.get("status", "?")
                order_id = order.get("id", "")[:16]
                usd_val = float(price) * remaining if price != "?" else 0

                self._print_status(
                    f"   {side} {remaining:.1f}/{original_size:.1f} shares @ ${price} "
                    f"(${usd_val:.2f}) | {status} | {order_id}…"
                )

        except Exception as exc:
            self._print_status(f"📋 Could not fetch open orders: {exc}")
            logger.warning("Could not fetch open orders: %s", exc)

    def _apply_tuned_params(self) -> None:
        """
        Push the optimizer's tuned parameters back into the live Config
        so all strategies pick them up on their next scan cycle.

        Only updates params that exist in the optimizer state.  Does NOT
        modify the optimizer's baseline — those are immutable.
        """
        if not self.optimizer or not self.optimizer.state.tuned_params:
            return

        tp = self.optimizer.state.tuned_params
        changed = []

        # Config-level params
        param_map = {
            "SIGNAL_MIN_EDGE": "SIGNAL_MIN_EDGE",
            "TAKE_PROFIT_PCT": "TAKE_PROFIT_PCT",
            "STOP_LOSS_PCT": "STOP_LOSS_PCT",
            "TRAILING_STOP_ACTIVATION": "TRAILING_STOP_ACTIVATION",
            "TRAILING_STOP_PCT": "TRAILING_STOP_PCT",
            "ARBITRAGE_MIN_EDGE": "ARBITRAGE_MIN_EDGE",
        }

        for opt_key, cfg_attr in param_map.items():
            if opt_key in tp:
                old_val = getattr(self.cfg, cfg_attr, None)
                new_val = tp[opt_key]
                if old_val is not None and abs(float(old_val) - float(new_val)) > 0.0001:
                    if cfg_attr == "MAX_HOLD_TIME":
                        setattr(self.cfg, cfg_attr, int(new_val))
                    else:
                        setattr(self.cfg, cfg_attr, float(new_val))
                    changed.append(f"{cfg_attr}: {old_val:.4f}→{new_val:.4f}")

        # MAX_HOLD_TIME (int)
        if "MAX_HOLD_TIME" in tp:
            old_val = self.cfg.MAX_HOLD_TIME
            new_val = int(tp["MAX_HOLD_TIME"])
            if abs(old_val - new_val) > 60:
                self.cfg.MAX_HOLD_TIME = new_val
                changed.append(f"MAX_HOLD_TIME: {old_val}s→{new_val}s")

        if changed:
            logger.info("Optimizer → config: %s", ", ".join(changed))

    def shutdown(self) -> None:
        """Gracefully stop the bot."""
        if self._shutdown:
            return
        logger.info("Shutting down bot…")
        self._shutdown = True

        # Save optimizer state
        if self.optimizer:
            try:
                self.optimizer._save_state()
                logger.info("Optimizer state saved.")
            except Exception as exc:
                logger.warning("Error saving optimizer state: %s", exc)

        # Print trade history report on clean shutdown
        if self.trade_history:
            try:
                self.trade_history.print_report()
            except Exception as exc:
                logger.warning("Error printing trade history report: %s", exc)


        # Write final P&L report
        if hasattr(self, "pnl_tracker"):
            try:
                report_path = self.pnl_tracker.write_report()
                logger.info("Final P\&L report saved to %s", report_path)
            except Exception as exc:
                logger.warning("Error writing P\&L report: %s", exc)
        # Cancel all open orders on clean shutdown
        if self.executor:
            try:
                self.executor.cancel_all_orders()
            except Exception as exc:
                logger.warning("Error cancelling orders during shutdown: %s", exc)

        # Save positions
        if self.tracker:
            try:
                self.tracker.save()
            except Exception as exc:
                logger.warning("Error saving positions during shutdown: %s", exc)

        # Stop WebSocket (fire-and-forget in daemon thread)
        if self.ws_manager:
            try:
                # Schedule stop coroutine
                asyncio.run_coroutine_threadsafe(
                    self.ws_manager.stop(),
                    asyncio.get_event_loop(),
                )
            except Exception:
                pass  # ws thread may already be down

        # Close shared HTTP session
        close_session()

        logger.info("Bot shutdown complete.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    """Parse args, build the bot, run the main loop."""
    args = parse_args()

    bot = TradingBot(args)

    # Register signal handlers for graceful shutdown
    def handle_signal(signum, frame):
        logger.info("Signal %s received, initiating shutdown…", signum)
        bot.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        bot.initialise()
        bot.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received.")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
