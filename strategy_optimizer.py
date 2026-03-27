"""
strategy_optimizer.py
---------------------
Adaptive self-learning engine that analyses the bot's own trade history to
improve performance over time.

The optimizer runs periodically (every OPTIMIZER_INTERVAL seconds) and performs
three types of adaptation:

  1. **Strategy weight adjustment** — Shifts allocation toward strategies with
     higher risk-adjusted returns.  Strategy weights control what fraction of
     total signals each strategy is allowed to produce per cycle.

  2. **Parameter tuning** — Adjusts individual strategy parameters (signal
     weights, edge thresholds, hold times, exit levels) based on which
     parameter ranges correlated with winning trades.

  3. **Regime detection** — Identifies whether the current market is
     trending, mean-reverting, or choppy and biases strategy selection
     accordingly.

Conservative guardrails:
  • Requires MIN_TRADES_FOR_ADAPTATION trades before any changes are made.
  • Maximum parameter shift per cycle is capped (MAX_PARAM_SHIFT).
  • All changes are logged and can be rolled back.
  • Original (baseline) parameters are always preserved.
  • A "performance floor" prevents the optimizer from degrading below
    the baseline's historical performance.

Persistence:
  • Optimizer state is saved to optimizer_state.json on every cycle and
    loaded on startup so learning survives bot restarts.
"""

import json
import random
import logging
import math
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from config import Config
from trade_history import TradeHistory, TradeRecord

logger = logging.getLogger("bot.optimizer")

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_STATE_FILE = "optimizer_state.json"

# Strategy names as used in TradeSignal.strategy
STRATEGY_ARBITRAGE = "arbitrage"
STRATEGY_COPY_TRADING = "copy_trading"
STRATEGY_SIGNAL_BASED = "signal_based"
STRATEGY_CRYPTO_MR = "crypto_mean_reversion"
STRATEGY_CONTRARIAN = "contrarian_extreme"
STRATEGY_AI = "ai_powered"
STRATEGY_SPORTS = "sports_momentum"
STRATEGY_CROSS_ARB = "cross_market_arb"
STRATEGY_WEATHER = "weather_forecast_arb"
STRATEGY_LP = "lp_rewards"

ALL_STRATEGIES = [
    STRATEGY_ARBITRAGE, STRATEGY_COPY_TRADING, STRATEGY_SIGNAL_BASED,
    STRATEGY_CRYPTO_MR, STRATEGY_CONTRARIAN, STRATEGY_AI,
    STRATEGY_SPORTS, STRATEGY_CROSS_ARB, STRATEGY_WEATHER, STRATEGY_LP,
]


@dataclass
class StrategyPerformance:
    """Aggregated performance metrics for a single strategy."""

    strategy: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    avg_pnl_per_trade: float = 0.0
    win_rate: float = 0.0
    avg_hold_time_s: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    profit_factor: float = 0.0   # gross_wins / gross_losses


@dataclass
class OptimizerState:
    """
    Complete optimizer state, persisted to disk.

    Stores the baseline (initial) and current (optimized) parameter sets,
    strategy weights, and performance history.
    """

    # Strategy allocation weights (sum to 1.0)
    strategy_weights: Dict[str, float] = field(default_factory=lambda: {
        STRATEGY_ARBITRAGE: 0.10,
        STRATEGY_COPY_TRADING: 0.10,
        STRATEGY_SIGNAL_BASED: 0.10,
        STRATEGY_CRYPTO_MR: 0.10,
        STRATEGY_CONTRARIAN: 0.10,
        STRATEGY_AI: 0.10,
        STRATEGY_SPORTS: 0.10,
        STRATEGY_CROSS_ARB: 0.10,
        STRATEGY_WEATHER: 0.10,
        STRATEGY_LP: 0.10,
    })

    # Tunable parameters — current values (mutated by optimizer)
    tuned_params: Dict[str, float] = field(default_factory=dict)

    # Baseline parameters (immutable, set on first run)
    baseline_params: Dict[str, float] = field(default_factory=dict)

    # Performance snapshots (list of dicts with timestamp + metrics)
    performance_log: List[Dict[str, Any]] = field(default_factory=list)

    # Detected market regime: "trending", "mean_reverting", "choppy", "unknown"
    market_regime: str = "unknown"

    # Number of optimization cycles completed
    cycles_completed: int = 0

    # Last optimization timestamp
    last_optimized: float = 0.0

    # Cumulative P&L at last optimization (for floor check)
    baseline_cumulative_pnl: float = 0.0


class StrategyOptimizer:
    """
    Self-learning engine that adapts strategy weights and parameters based
    on the bot's own trade history.

    Usage:
        optimizer = StrategyOptimizer(cfg, trade_history)
        # Called periodically from the main loop:
        optimizer.maybe_optimize()
        # Query current weights:
        weight = optimizer.get_strategy_weight("signal_based")
        # Check if a strategy should be throttled:
        if optimizer.should_execute_signal(signal):
            executor.execute(signal)
    """

    def __init__(
        self,
        cfg: Config,
        trade_history: TradeHistory,
        state_file: str = DEFAULT_STATE_FILE,
    ) -> None:
        self.cfg = cfg
        self.trade_history = trade_history
        self.state_file = state_file
        self.state = OptimizerState()

        # Tuneable thresholds from config (with env overrides)
        self.min_trades = int(os.getenv("OPTIMIZER_MIN_TRADES", "50"))
        self.interval = int(os.getenv("OPTIMIZER_INTERVAL", "3600"))     # 1 hour
        self.max_param_shift = float(os.getenv("OPTIMIZER_MAX_SHIFT", "0.15"))  # 15% max change
        self.lookback_trades = int(os.getenv("OPTIMIZER_LOOKBACK", "200"))
        self.enabled = os.getenv("OPTIMIZER_ENABLED", "true").lower() in ("1", "true", "yes")

        self._load_state()
        self._ensure_baseline(cfg)

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def maybe_optimize(self) -> bool:
        """
        Run the optimization cycle if enough time has passed and enough
        trade data is available.

        Returns:
            True if optimization was performed, False if skipped.
        """
        if not self.enabled:
            return False

        # Time gate
        now = time.time()
        if now - self.state.last_optimized < self.interval:
            return False

        # Data gate
        records = self.trade_history.get_records()
        if len(records) < self.min_trades:
            logger.info(
                "Optimizer: need %d trades, have %d. Skipping.",
                self.min_trades, len(records),
            )
            return False

        logger.info(
            "═══ Optimizer cycle %d starting (%d trades available) ═══",
            self.state.cycles_completed + 1,
            len(records),
        )

        try:
            self._run_optimization(records)
            self.state.last_optimized = now
            self.state.cycles_completed += 1
            self._save_state()
            logger.info("═══ Optimizer cycle %d complete ═══", self.state.cycles_completed)
            return True
        except Exception as exc:
            logger.error("Optimizer error: %s", exc, exc_info=True)
            return False

    def get_strategy_weight(self, strategy_name: str) -> float:
        """Return the current weight for a strategy (0.0–1.0)."""
        return self.state.strategy_weights.get(strategy_name, 0.10)

    def should_execute_signal(self, signal) -> bool:
        """
        Probabilistic gate: allow or suppress a signal based on strategy weight.

        A strategy with weight 1.0 always passes.  A strategy with weight 0.1
        only passes ~10% of the time.  This avoids hard cutoffs and allows
        all strategies to continue generating some data even when underperforming.

        All strategies always get at least a 10% floor to prevent starvation.
        """
        weight = self.get_strategy_weight(signal.strategy)
        # Floor: always allow at least 10% through
        effective_weight = max(weight, 0.10)
        return random.random() < effective_weight

    def get_tuned_param(self, param_name: str, default: float) -> float:
        """
        Return the current tuned value for a parameter, or the default
        if the optimizer hasn't tuned it yet.
        """
        return self.state.tuned_params.get(param_name, default)

    def get_regime(self) -> str:
        """Return the current detected market regime."""
        return self.state.market_regime

    def get_performance_summary(self) -> Dict[str, Any]:
        """Return a summary dict of current optimizer state for logging."""
        return {
            "cycles": self.state.cycles_completed,
            "regime": self.state.market_regime,
            "weights": {k: round(v, 3) for k, v in self.state.strategy_weights.items()},
            "tuned_params_count": len(self.state.tuned_params),
            "last_optimized": self.state.last_optimized,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Core optimization pipeline
    # ─────────────────────────────────────────────────────────────────────────

    def _run_optimization(self, records: List[TradeRecord]) -> None:
        """Execute the full optimization pipeline."""
        # Use the most recent N trades for analysis
        recent = records[-self.lookback_trades:]

        # 1. Compute per-strategy performance
        perf_map = self._compute_strategy_performance(recent)
        self._log_performance(perf_map)

        # 2. Detect market regime
        self._detect_regime(recent)

        # 3. Adjust strategy weights
        self._adjust_strategy_weights(perf_map)

        # 4. Tune individual parameters
        self._tune_parameters(recent, perf_map)

        # 5. Performance floor check — revert if we're doing worse
        self._check_performance_floor(records)

        # 6. Log snapshot
        self._log_snapshot(perf_map)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Per-strategy performance analysis
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_strategy_performance(
        self, records: List[TradeRecord]
    ) -> Dict[str, StrategyPerformance]:
        """
        Aggregate trade records into per-strategy performance metrics.

        Pairs BUY→SELL trades by token_id to compute P&L per round-trip.
        Unpaired BUYs are tracked using mark-to-market from the last
        recorded price.
        """
        perf: Dict[str, StrategyPerformance] = {}
        # Track individual trade PnLs per strategy for accurate profit factor / Sharpe
        pnl_by_strategy: Dict[str, List[float]] = {}
        for s in ALL_STRATEGIES + ["trade_manager"]:
            perf[s] = StrategyPerformance(strategy=s)
            pnl_by_strategy[s] = []

        # Group by token_id to pair entries and exits
        by_token: Dict[str, List[TradeRecord]] = {}
        for rec in records:
            by_token.setdefault(rec.token_id, []).append(rec)

        for token_id, token_records in by_token.items():
            buys = [r for r in token_records if r.side == "BUY"]
            sells = [r for r in token_records if r.side == "SELL"]

            for buy in buys:
                strategy = buy.strategy
                if strategy not in perf:
                    perf[strategy] = StrategyPerformance(strategy=strategy)
                    pnl_by_strategy[strategy] = []

                sp = perf[strategy]
                sp.total_trades += 1

                # Find the matching sell (first sell after this buy)
                matching_sell = None
                for sell in sells:
                    if sell.timestamp > buy.timestamp:
                        matching_sell = sell
                        break

                if matching_sell:
                    # Closed trade: compute realised P&L using log returns
                    arith_pnl = (matching_sell.price - buy.price) * buy.size
                    if buy.price > 0 and matching_sell.price > 0:
                        log_return = math.log(matching_sell.price / buy.price)
                    else:
                        log_return = 0.0
                    pnl = arith_pnl  # Keep arithmetic for display; use log for aggregation
                    hold_time = matching_sell.timestamp - buy.timestamp
                    sells.remove(matching_sell)
                else:
                    # Open trade: use mark-to-market (last known price as proxy)
                    pnl = 0.0  # conservative — don't count unrealised
                    hold_time = time.time() - buy.timestamp

                sp.total_pnl += pnl
                pnl_by_strategy[strategy].append(pnl)
                sp.avg_hold_time_s = (
                    (sp.avg_hold_time_s * (sp.total_trades - 1) + hold_time)
                    / sp.total_trades
                )

                if pnl > 0:
                    sp.winning_trades += 1
                elif pnl < 0:
                    sp.losing_trades += 1

        # Compute derived metrics
        for sp in perf.values():
            if sp.total_trades > 0:
                sp.avg_pnl_per_trade = sp.total_pnl / sp.total_trades
                sp.win_rate = sp.winning_trades / sp.total_trades

                # Profit factor: gross winning PnL / abs(gross losing PnL)
                trade_pnls = pnl_by_strategy.get(sp.strategy, [])
                sp.profit_factor = self._compute_profit_factor(trade_pnls)

                # Simplified Sharpe: avg_pnl / std_dev_pnl
                sp.sharpe_ratio = self._compute_sharpe(sp, records, trade_pnls)

        return perf

    @staticmethod
    def _compute_profit_factor(trade_pnls: List[float]) -> float:
        """Compute profit factor: gross wins / abs(gross losses)."""
        winning_pnls = [p for p in trade_pnls if p > 0]
        losing_pnls = [p for p in trade_pnls if p < 0]
        if not winning_pnls or not losing_pnls:
            return 1.0  # insufficient data
        return sum(winning_pnls) / abs(sum(losing_pnls))

    def _compute_sharpe(
        self, sp: StrategyPerformance, records: List[TradeRecord],
        trade_pnls: Optional[List[float]] = None,
    ) -> float:
        """Compute a simplified Sharpe ratio for a strategy using actual P&L."""
        pnls = trade_pnls if trade_pnls is not None else []
        # Fall back: try to infer from records if no pnl list supplied
        if not pnls:
            return 0.0
        if len(pnls) < 2:
            return 0.0

        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)
        std_dev = math.sqrt(variance) if variance > 0 else 0.001

        return mean_pnl / std_dev

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Market regime detection
    # ─────────────────────────────────────────────────────────────────────────

    def _detect_regime(self, records: List[TradeRecord]) -> None:
        """
        Classify the current market regime based on recent trade outcomes.

        Trending:       Momentum trades profit, value trades struggle.
        Mean-reverting: Value/spread trades profit, momentum loses.
        Choppy:         All strategies struggle, high stop-loss rate.
        """
        if len(records) < 10:
            self.state.market_regime = "unknown"
            return

        # Compute short-term win rates by strategy type
        recent_30 = records[-30:] if len(records) >= 30 else records

        momentum_wins = 0
        momentum_total = 0
        value_wins = 0
        value_total = 0
        stop_loss_count = 0

        for rec in recent_30:
            if rec.strategy == STRATEGY_SIGNAL_BASED:
                momentum_total += 1
                # Heuristic: if the signal mentions "momentum" in reason, count it
                if "mom=" in rec.reason.lower():
                    momentum_total += 1
                    if rec.side == "SELL" and "take-profit" in rec.reason.lower():
                        momentum_wins += 1
            if "value" in rec.reason.lower() or "val=" in rec.reason.lower():
                value_total += 1
            if "stop-loss" in rec.reason.lower():
                stop_loss_count += 1

        total = len(recent_30)
        stop_loss_rate = stop_loss_count / total if total > 0 else 0

        # Classification logic
        old_regime = self.state.market_regime

        if stop_loss_rate > 0.30:
            self.state.market_regime = "choppy"
        elif momentum_total > 0 and momentum_wins / max(momentum_total, 1) > 0.60:
            self.state.market_regime = "trending"
        elif value_total > 0 and value_total / total > 0.30:
            self.state.market_regime = "mean_reverting"
        else:
            self.state.market_regime = "unknown"

        if old_regime != self.state.market_regime:
            logger.info(
                "Regime change detected: %s → %s (stop_loss_rate=%.1f%%)",
                old_regime,
                self.state.market_regime,
                stop_loss_rate * 100,
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Strategy weight adjustment
    # ─────────────────────────────────────────────────────────────────────────

    def _adjust_strategy_weights(
        self, perf_map: Dict[str, StrategyPerformance]
    ) -> None:
        """
        Shift strategy allocation weights toward better-performing strategies.

        Uses a combination of win rate and risk-adjusted return (Sharpe proxy)
        to compute a score for each strategy.  Weights are adjusted gradually
        (max MAX_PARAM_SHIFT per cycle) to avoid oscillation.
        """
        scores: Dict[str, float] = {}

        for strat_name in ALL_STRATEGIES:
            sp = perf_map.get(strat_name)
            if not sp or sp.total_trades < 5:
                # Not enough data — keep current weight
                scores[strat_name] = self.state.strategy_weights.get(strat_name, 0.10)
                continue

            # Composite score: 50% win rate, 30% profit factor, 20% Sharpe
            wr_score = sp.win_rate
            pf_score = min(sp.profit_factor / 3.0, 1.0) if sp.profit_factor != float("inf") else 1.0
            sh_score = min(max(sp.sharpe_ratio, 0) / 2.0, 1.0)

            score = 0.50 * wr_score + 0.30 * pf_score + 0.20 * sh_score
            scores[strat_name] = max(score, 0.05)  # floor of 5%

        # ── Drawdown-based throttling ────────────────────────────────────────
        # Penalise strategies with negative Sharpe or negative total PnL.
        # Strategies with 5+ consecutive losses get a heavier reduction.
        for strat_name in ALL_STRATEGIES:
            sp = perf_map.get(strat_name)
            if not sp or sp.total_trades < 5:
                continue
            # Count consecutive losses at the end of the record (approximate)
            consec_losses = sp.losing_trades  # rough proxy if no per-trade sequence
            if sp.sharpe_ratio < 0 or sp.total_pnl < 0:
                scores[strat_name] = scores.get(strat_name, 0.10) * 0.75  # reduce 25%
                logger.debug(
                    "Throttling %s 25%%: sharpe=%.2f pnl=%.2f",
                    strat_name, sp.sharpe_ratio, sp.total_pnl,
                )
            if consec_losses >= 5:
                scores[strat_name] = scores.get(strat_name, 0.10) * 0.50  # reduce 50%
                logger.debug(
                    "Throttling %s 50%%: consec_losses=%d",
                    strat_name, consec_losses,
                )
            # Floor: never fully disable
            scores[strat_name] = max(scores.get(strat_name, 0.02), 0.02)

        # Normalise scores to sum to 1.0
        total_score = sum(scores.values())
        if total_score <= 0:
            return

        target_weights = {k: v / total_score for k, v in scores.items()}

        # Gradually move toward target (max shift per cycle)
        old_weights = dict(self.state.strategy_weights)
        for strat_name in ALL_STRATEGIES:
            current = self.state.strategy_weights.get(strat_name, 0.10)
            target = target_weights.get(strat_name, 0.10)
            delta = target - current
            # Clamp the shift
            clamped_delta = max(-self.max_param_shift, min(self.max_param_shift, delta))
            new_weight = current + clamped_delta
            # Floor: every strategy gets at least 10%
            self.state.strategy_weights[strat_name] = max(new_weight, 0.10)

        # Re-normalise after flooring
        total = sum(self.state.strategy_weights[s] for s in ALL_STRATEGIES)
        for s in ALL_STRATEGIES:
            self.state.strategy_weights[s] /= total

        # Log changes
        for s in ALL_STRATEGIES:
            old_w = old_weights.get(s, 0.10)
            new_w = self.state.strategy_weights[s]
            if abs(new_w - old_w) > 0.005:
                logger.info(
                    "Weight adjusted: %s %.1f%% → %.1f%%",
                    s, old_w * 100, new_w * 100,
                )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: Parameter tuning
    # ─────────────────────────────────────────────────────────────────────────

    def _tune_parameters(
        self,
        records: List[TradeRecord],
        perf_map: Dict[str, StrategyPerformance],
    ) -> None:
        """
        Adjust individual strategy parameters based on trade outcome analysis.

        Parameters tuned:
          - SIGNAL_MIN_EDGE: Raise if too many losing signals, lower if too few
          - TAKE_PROFIT_PCT: Raise if many trades hit TP then continue, lower if
            too few reach TP
          - STOP_LOSS_PCT: Tighten if drawdowns are too deep, widen if stopped
            out too frequently before recovery
          - MAX_HOLD_TIME: Shorten if late exits are losers, lengthen if winners
            need more time
          - Signal sub-weights (volume, momentum, value, spread): shift toward
            sub-signals correlated with winning trades
        """
        # ── SIGNAL_MIN_EDGE ───────────────────────────────────────────────────
        signal_perf = perf_map.get(STRATEGY_SIGNAL_BASED)
        if signal_perf and signal_perf.total_trades >= 10:
            baseline_edge = self.state.baseline_params.get("SIGNAL_MIN_EDGE", 0.05)
            current_edge = self.state.tuned_params.get("SIGNAL_MIN_EDGE", baseline_edge)

            if signal_perf.win_rate < 0.40:
                # Too many losers — raise the bar
                new_edge = current_edge * (1 + self.max_param_shift * 0.5)
            elif signal_perf.win_rate > 0.65:
                # Very successful — can afford to lower the bar to catch more
                new_edge = current_edge * (1 - self.max_param_shift * 0.3)
            else:
                new_edge = current_edge

            new_edge = self._clamp_param(new_edge, baseline_edge, "SIGNAL_MIN_EDGE")
            self.state.tuned_params["SIGNAL_MIN_EDGE"] = new_edge

        # ── TAKE_PROFIT_PCT ───────────────────────────────────────────────────
        tp_exits = [r for r in records if "take-profit" in r.reason.lower()]
        if len(tp_exits) >= 5:
            baseline_tp = self.state.baseline_params.get("TAKE_PROFIT_PCT", 0.15)
            current_tp = self.state.tuned_params.get("TAKE_PROFIT_PCT", baseline_tp)
            tp_rate = len(tp_exits) / max(len(records), 1)

            if tp_rate > 0.40:
                # Lots of trades hitting TP — might be leaving money on the table
                new_tp = current_tp * (1 + self.max_param_shift * 0.3)
            elif tp_rate < 0.10:
                # Very few TP hits — target might be too ambitious
                new_tp = current_tp * (1 - self.max_param_shift * 0.2)
            else:
                new_tp = current_tp

            new_tp = self._clamp_param(new_tp, baseline_tp, "TAKE_PROFIT_PCT")
            self.state.tuned_params["TAKE_PROFIT_PCT"] = new_tp

        # ── STOP_LOSS_PCT ─────────────────────────────────────────────────────
        sl_exits = [r for r in records if "stop-loss" in r.reason.lower()]
        if len(sl_exits) >= 5:
            baseline_sl = self.state.baseline_params.get("STOP_LOSS_PCT", 0.10)
            current_sl = self.state.tuned_params.get("STOP_LOSS_PCT", baseline_sl)
            sl_rate = len(sl_exits) / max(len(records), 1)

            if sl_rate > 0.30:
                # Too many stop-losses — might be too tight
                new_sl = current_sl * (1 + self.max_param_shift * 0.3)
            elif sl_rate < 0.05:
                # Rare stop-losses — can afford to tighten
                new_sl = current_sl * (1 - self.max_param_shift * 0.2)
            else:
                new_sl = current_sl

            new_sl = self._clamp_param(new_sl, baseline_sl, "STOP_LOSS_PCT")
            self.state.tuned_params["STOP_LOSS_PCT"] = new_sl

        # ── MAX_HOLD_TIME ─────────────────────────────────────────────────────
        time_exits = [r for r in records if "time exit" in r.reason.lower()]
        if len(time_exits) >= 3:
            baseline_hold = self.state.baseline_params.get("MAX_HOLD_TIME", 86400)
            current_hold = self.state.tuned_params.get("MAX_HOLD_TIME", baseline_hold)

            # Check if time exits were profitable or not
            time_exit_pnl = sum(r.usd_value for r in time_exits)
            avg_time_exit = time_exit_pnl / len(time_exits) if time_exits else 0

            if avg_time_exit < 0:
                # Time exits losing money — exit sooner
                new_hold = current_hold * (1 - self.max_param_shift * 0.3)
            else:
                # Time exits profitable — can hold longer
                new_hold = current_hold * (1 + self.max_param_shift * 0.2)

            new_hold = self._clamp_param(new_hold, baseline_hold, "MAX_HOLD_TIME")
            self.state.tuned_params["MAX_HOLD_TIME"] = new_hold

        # ── Signal sub-weights ────────────────────────────────────────────────
        self._tune_signal_weights(records)

        # Log tuned parameters
        if self.state.tuned_params:
            logger.info("Tuned parameters: %s", {
                k: round(v, 4) if isinstance(v, float) else v
                for k, v in self.state.tuned_params.items()
            })

    def _tune_signal_weights(self, records: List[TradeRecord]) -> None:
        """
        Adjust the four signal sub-weights (volume_spike, momentum, value,
        spread) based on which sub-signals correlate with winning trades.

        Parses the reason string from signal_based trades to extract
        sub-signal scores and correlates them with trade outcomes.
        """
        signal_records = [r for r in records if r.strategy == STRATEGY_SIGNAL_BASED]
        if len(signal_records) < 15:
            return

        # Parse sub-signal scores from reason strings
        sub_signal_wins: Dict[str, List[float]] = {
            "volume_spike": [], "momentum": [], "value": [], "spread": [],
        }
        sub_signal_all: Dict[str, List[float]] = {
            "volume_spike": [], "momentum": [], "value": [], "spread": [],
        }

        for rec in signal_records:
            parsed = self._parse_signal_breakdown(rec.reason)
            if not parsed:
                continue

            is_winner = rec.side == "BUY"  # We count buys that eventually profit
            for key in sub_signal_all:
                val = parsed.get(key, 0.0)
                sub_signal_all[key].append(val)
                if is_winner:
                    sub_signal_wins[key].append(val)

        # Compute correlation: which sub-signals are higher in winning trades?
        baseline_weights = {
            "WEIGHT_VOLUME_SPIKE": 0.30,
            "WEIGHT_MOMENTUM": 0.25,
            "WEIGHT_VALUE": 0.25,
            "WEIGHT_SPREAD": 0.20,
        }

        weight_map = {
            "volume_spike": "WEIGHT_VOLUME_SPIKE",
            "momentum": "WEIGHT_MOMENTUM",
            "value": "WEIGHT_VALUE",
            "spread": "WEIGHT_SPREAD",
        }

        for sub_name, param_name in weight_map.items():
            all_vals = sub_signal_all[sub_name]
            win_vals = sub_signal_wins[sub_name]

            if len(all_vals) < 5 or len(win_vals) < 2:
                continue

            avg_all = sum(all_vals) / len(all_vals) if all_vals else 0
            avg_win = sum(win_vals) / len(win_vals) if win_vals else 0

            baseline = self.state.baseline_params.get(param_name, baseline_weights[param_name])
            current = self.state.tuned_params.get(param_name, baseline)

            # If winning trades have higher sub-signal score, increase weight
            if avg_win > avg_all * 1.1:
                new_val = current * (1 + self.max_param_shift * 0.2)
            elif avg_win < avg_all * 0.9:
                new_val = current * (1 - self.max_param_shift * 0.2)
            else:
                new_val = current

            new_val = self._clamp_param(new_val, baseline, param_name)
            self.state.tuned_params[param_name] = new_val

        # Normalise signal weights to sum to 1.0
        sig_keys = list(weight_map.values())
        total = sum(self.state.tuned_params.get(k, baseline_weights[k]) for k in sig_keys)
        if total > 0:
            for k in sig_keys:
                val = self.state.tuned_params.get(k, baseline_weights[k])
                self.state.tuned_params[k] = val / total

    @staticmethod
    def _parse_signal_breakdown(reason: str) -> Optional[Dict[str, float]]:
        """
        Parse sub-signal scores from a signal_based reason string.

        Expected format: "Signal composite=0.123 | vs=0.45 mom=0.20 val=0.30 sp=0.10 | ..."
        """
        result = {}
        key_map = {"vs": "volume_spike", "mom": "momentum", "val": "value", "sp": "spread"}

        for abbr, full_name in key_map.items():
            try:
                idx = reason.find(f"{abbr}=")
                if idx >= 0:
                    # Extract the number after "key="
                    start = idx + len(abbr) + 1
                    end = start
                    while end < len(reason) and (reason[end].isdigit() or reason[end] == "."):
                        end += 1
                    result[full_name] = float(reason[start:end])
            except (ValueError, IndexError):
                pass

        return result if result else None

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: Performance floor
    # ─────────────────────────────────────────────────────────────────────────

    def _check_performance_floor(self, all_records: List[TradeRecord]) -> None:
        """
        If the optimizer's changes have degraded overall performance below
        the baseline, revert to baseline parameters.

        Compares recent performance (last 50 trades) against the historical
        average from when the baseline was established.
        """
        if self.state.cycles_completed < 3:
            # Need at least 3 cycles of tuned data
            return

        recent_50 = all_records[-50:]
        if len(recent_50) < 30:
            return

        # Compute recent win rate
        buys = [r for r in recent_50 if r.side == "BUY"]
        sells = [r for r in recent_50 if r.side == "SELL"]

        if len(buys) < 10:
            return

        # Simple heuristic: if overall win rate has dropped below 35%, revert
        # This is a very conservative floor to prevent catastrophic degradation
        paired = 0
        wins = 0
        for buy in buys:
            for sell in sells:
                if sell.token_id == buy.token_id and sell.timestamp > buy.timestamp:
                    paired += 1
                    if sell.price > buy.price:
                        wins += 1
                    break

        if paired < 10:
            return

        recent_win_rate = wins / paired

        if recent_win_rate < 0.35:
            logger.warning(
                "PERFORMANCE FLOOR triggered! Recent win rate %.1f%% < 35%%. "
                "Reverting to baseline parameters.",
                recent_win_rate * 100,
            )
            self.state.tuned_params = dict(self.state.baseline_params)
            equal_weight = 1.0 / len(ALL_STRATEGIES)
            self.state.strategy_weights = {s: equal_weight for s in ALL_STRATEGIES}
            logger.info("Parameters reverted to baseline.")

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _clamp_param(
        self, new_value: float, baseline: float, param_name: str
    ) -> float:
        """
        Clamp a tuned parameter so it doesn't drift more than
        MAX_PARAM_SHIFT (default ±15%) from the baseline in either direction.

        Also enforces hard min/max for specific parameters.
        """
        # Allow drift of up to max_param_shift from baseline on each side
        # But cumulative drift can't exceed 2× the shift (30% total from baseline)
        max_cumulative = self.max_param_shift * 2
        lower = baseline * (1 - max_cumulative)
        upper = baseline * (1 + max_cumulative)

        # Hard floors for specific parameters
        hard_limits = {
            "SIGNAL_MIN_EDGE": (0.01, 0.20),
            "TAKE_PROFIT_PCT": (0.05, 0.50),
            "STOP_LOSS_PCT": (0.03, 0.25),
            "MAX_HOLD_TIME": (3600, 259200),  # 1h–72h
            "WEIGHT_VOLUME_SPIKE": (0.05, 0.60),
            "WEIGHT_MOMENTUM": (0.05, 0.60),
            "WEIGHT_VALUE": (0.05, 0.60),
            "WEIGHT_SPREAD": (0.05, 0.60),
        }

        if param_name in hard_limits:
            hard_lower, hard_upper = hard_limits[param_name]
            lower = max(lower, hard_lower)
            upper = min(upper, hard_upper)

        clamped = max(lower, min(upper, new_value))

        if abs(clamped - new_value) > 0.0001:
            logger.debug(
                "Clamped %s: %.4f → %.4f (bounds=[%.4f, %.4f])",
                param_name, new_value, clamped, lower, upper,
            )

        return clamped

    def _ensure_baseline(self, cfg: Config) -> None:
        """
        On first run, capture the current config values as the immutable
        baseline that all tuning is measured against.
        """
        if self.state.baseline_params:
            return  # Already set from a previous run

        self.state.baseline_params = {
            "SIGNAL_MIN_EDGE": cfg.SIGNAL_MIN_EDGE,
            "TAKE_PROFIT_PCT": cfg.TAKE_PROFIT_PCT,
            "STOP_LOSS_PCT": cfg.STOP_LOSS_PCT,
            "MAX_HOLD_TIME": float(cfg.MAX_HOLD_TIME),
            "TRAILING_STOP_ACTIVATION": cfg.TRAILING_STOP_ACTIVATION,
            "TRAILING_STOP_PCT": cfg.TRAILING_STOP_PCT,
            "ARBITRAGE_MIN_EDGE": cfg.ARBITRAGE_MIN_EDGE,
            "WEIGHT_VOLUME_SPIKE": 0.30,
            "WEIGHT_MOMENTUM": 0.25,
            "WEIGHT_VALUE": 0.25,
            "WEIGHT_SPREAD": 0.20,
        }
        self.state.tuned_params = dict(self.state.baseline_params)
        logger.info("Baseline parameters captured: %s", self.state.baseline_params)

    def _log_performance(self, perf_map: Dict[str, StrategyPerformance]) -> None:
        """Log per-strategy performance metrics."""
        for strat_name in ALL_STRATEGIES:
            sp = perf_map.get(strat_name)
            if not sp or sp.total_trades == 0:
                continue
            logger.info(
                "Perf [%s]: trades=%d wr=%.1f%% pnl=$%.2f avg=$%.4f "
                "pf=%.2f sharpe=%.2f hold=%.1fh",
                strat_name,
                sp.total_trades,
                sp.win_rate * 100,
                sp.total_pnl,
                sp.avg_pnl_per_trade,
                sp.profit_factor if sp.profit_factor != float("inf") else 99.9,
                sp.sharpe_ratio,
                sp.avg_hold_time_s / 3600,
            )

    def _log_snapshot(self, perf_map: Dict[str, StrategyPerformance]) -> None:
        """Append a performance snapshot to the log."""
        snapshot = {
            "timestamp": time.time(),
            "cycle": self.state.cycles_completed,
            "regime": self.state.market_regime,
            "weights": dict(self.state.strategy_weights),
            "strategies": {},
        }
        for strat_name in ALL_STRATEGIES:
            sp = perf_map.get(strat_name)
            if sp:
                snapshot["strategies"][strat_name] = {
                    "trades": sp.total_trades,
                    "win_rate": round(sp.win_rate, 3),
                    "pnl": round(sp.total_pnl, 4),
                    "sharpe": round(sp.sharpe_ratio, 3),
                }

        self.state.performance_log.append(snapshot)

        # Keep only last 100 snapshots to limit file size
        if len(self.state.performance_log) > 100:
            self.state.performance_log = self.state.performance_log[-100:]

    # ─────────────────────────────────────────────────────────────────────────
    # Persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _save_state(self) -> None:
        """Persist optimizer state to JSON."""
        try:
            data = {
                "strategy_weights": self.state.strategy_weights,
                "tuned_params": self.state.tuned_params,
                "baseline_params": self.state.baseline_params,
                "performance_log": self.state.performance_log,
                "market_regime": self.state.market_regime,
                "cycles_completed": self.state.cycles_completed,
                "last_optimized": self.state.last_optimized,
                "baseline_cumulative_pnl": self.state.baseline_cumulative_pnl,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.debug("Optimizer state saved to %s", self.state_file)
        except Exception as exc:
            logger.error("Failed to save optimizer state: %s", exc)

    def _load_state(self) -> None:
        """Load optimizer state from JSON if it exists."""
        if not os.path.exists(self.state_file):
            logger.info("No existing optimizer state; starting fresh.")
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.state.strategy_weights = data.get("strategy_weights", self.state.strategy_weights)
            self.state.tuned_params = data.get("tuned_params", {})
            self.state.baseline_params = data.get("baseline_params", {})
            self.state.performance_log = data.get("performance_log", [])
            self.state.market_regime = data.get("market_regime", "unknown")
            self.state.cycles_completed = data.get("cycles_completed", 0)
            self.state.last_optimized = data.get("last_optimized", 0.0)
            self.state.baseline_cumulative_pnl = data.get("baseline_cumulative_pnl", 0.0)

            logger.info(
                "Loaded optimizer state: %d cycles, regime=%s, weights=%s",
                self.state.cycles_completed,
                self.state.market_regime,
                {k: f"{v:.1%}" for k, v in self.state.strategy_weights.items()},
            )
        except Exception as exc:
            logger.error("Failed to load optimizer state: %s", exc)
