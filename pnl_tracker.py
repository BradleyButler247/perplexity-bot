"""
pnl_tracker.py
--------------
Tracks and reports P&L across all positions and strategies.

Generates a daily summary file (pnl_report_YYYY-MM-DD.txt) and maintains
running totals for the current session.

Reports include:
  - Open positions with current unrealised P&L
  - Closed/resolved trades with realised P&L
  - Per-strategy performance breakdown
  - Win/loss counts and win rate
  - Daily, weekly, and all-time P&L
  - Best and worst trades

Usage:
    tracker = PnLTracker(position_tracker, trade_history)
    tracker.update()           # Call once per cycle
    tracker.write_report()     # Write daily summary to file
    print(tracker.summary())   # Quick one-line summary
"""

import datetime
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from position_tracker import Position, PositionTracker
from trade_history import TradeHistory, TradeRecord

logger = logging.getLogger("bot.pnl_tracker")

REPORT_DIR = "reports"


@dataclass
class StrategyStats:
    """Aggregated stats for a single strategy."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_invested: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    best_trade_desc: str = ""
    worst_trade_desc: str = ""

    @property
    def win_rate(self) -> float:
        resolved = self.wins + self.losses
        return self.wins / resolved if resolved > 0 else 0.0

    @property
    def roi(self) -> float:
        return self.total_pnl / self.total_invested if self.total_invested > 0 else 0.0


class PnLTracker:
    """
    Tracks profit and loss across the bot's lifetime.

    Call update() each cycle to refresh position prices.
    Call write_report() periodically to save a human-readable summary.
    """

    def __init__(
        self,
        position_tracker: PositionTracker,
        trade_history: Optional[TradeHistory] = None,
    ) -> None:
        self._tracker = position_tracker
        self._history = trade_history
        self._session_start = time.time()
        self._last_report_date: str = ""
        self._cycle_count: int = 0

        # Ensure report directory exists
        os.makedirs(REPORT_DIR, exist_ok=True)

    def update(self) -> None:
        """Call once per cycle to track state."""
        self._cycle_count += 1

        # Auto-write daily report at midnight UTC
        today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._last_report_date:
            if self._last_report_date:  # Don't write on first cycle
                self.write_report(self._last_report_date)
            self._last_report_date = today

    def summary(self) -> str:
        """Return a compact one-line P&L summary for the cycle log."""
        positions = self._tracker.get_all_positions()
        realised = self._tracker.realised_pnl

        unrealised = 0.0
        for pos in positions:
            if pos.current_price > 0 and pos.entry_price > 0:
                unrealised += (pos.current_price - pos.entry_price) * pos.size

        total = realised + unrealised
        n_open = len(positions)

        return (
            f"P&L: ${total:+.2f} "
            f"(realised=${realised:+.2f} unrealised=${unrealised:+.2f}) | "
            f"{n_open} open"
        )

    def write_report(self, date_str: Optional[str] = None) -> str:
        """
        Write a detailed P&L report to a text file.

        Args:
            date_str: Date for the report filename (default: today UTC).

        Returns:
            Path to the report file.
        """
        if not date_str:
            date_str = datetime.datetime.utcnow().strftime("%Y-%m-%d")

        report_path = os.path.join(REPORT_DIR, f"pnl_report_{date_str}.txt")

        lines = []
        lines.append(f"{'=' * 60}")
        lines.append(f"  P&L Report — {date_str}")
        lines.append(f"  Generated: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"{'=' * 60}")
        lines.append("")

        # ── Overall summary ──────────────────────────────────────────
        positions = self._tracker.get_all_positions()
        realised = self._tracker.realised_pnl

        unrealised = 0.0
        for pos in positions:
            if pos.current_price > 0 and pos.entry_price > 0:
                unrealised += (pos.current_price - pos.entry_price) * pos.size

        total = realised + unrealised

        lines.append("SUMMARY")
        lines.append(f"  Total P&L:      ${total:+.2f}")
        lines.append(f"  Realised P&L:   ${realised:+.2f}")
        lines.append(f"  Unrealised P&L: ${unrealised:+.2f}")
        lines.append(f"  Open positions: {len(positions)}")
        lines.append(f"  Session cycles: {self._cycle_count}")
        lines.append("")

        # ── Open positions detail ────────────────────────────────────
        if positions:
            lines.append("OPEN POSITIONS")
            lines.append(f"  {'Outcome':<25} {'Entry':>8} {'Current':>8} {'Size':>8} {'P&L':>10} {'P&L%':>8}")
            lines.append(f"  {'-' * 73}")

            for pos in sorted(positions, key=lambda p: (p.current_price - p.entry_price) * p.size, reverse=True):
                pnl = (pos.current_price - pos.entry_price) * pos.size if pos.current_price > 0 else 0
                pnl_pct = ((pos.current_price / pos.entry_price) - 1) * 100 if pos.entry_price > 0 and pos.current_price > 0 else 0
                outcome = pos.outcome[:25] if pos.outcome else pos.token_id[:16]
                lines.append(
                    f"  {outcome:<25} ${pos.entry_price:>7.3f} ${pos.current_price:>7.3f} "
                    f"{pos.size:>8.1f} ${pnl:>+9.2f} {pnl_pct:>+7.1f}%"
                )
            lines.append("")

        # ── Strategy breakdown ───────────────────────────────────────
        strategy_stats = self._compute_strategy_stats()
        if strategy_stats:
            lines.append("STRATEGY PERFORMANCE")
            lines.append(f"  {'Strategy':<25} {'Trades':>7} {'Wins':>6} {'WR%':>6} {'P&L':>10} {'ROI':>8}")
            lines.append(f"  {'-' * 68}")

            for name, stats in sorted(strategy_stats.items(), key=lambda x: x[1].total_pnl, reverse=True):
                wr_str = f"{stats.win_rate:.0%}" if stats.wins + stats.losses > 0 else "n/a"
                roi_str = f"{stats.roi:.1%}" if stats.total_invested > 0 else "n/a"
                lines.append(
                    f"  {name:<25} {stats.trades:>7} {stats.wins:>6} {wr_str:>6} "
                    f"${stats.total_pnl:>+9.2f} {roi_str:>8}"
                )

            lines.append("")

            # Best and worst trades
            all_stats = list(strategy_stats.values())
            best = max(all_stats, key=lambda s: s.best_trade_pnl)
            worst = min(all_stats, key=lambda s: s.worst_trade_pnl)

            if best.best_trade_pnl > 0:
                lines.append(f"  Best trade:  ${best.best_trade_pnl:+.2f} | {best.best_trade_desc}")
            if worst.worst_trade_pnl < 0:
                lines.append(f"  Worst trade: ${worst.worst_trade_pnl:+.2f} | {worst.worst_trade_desc}")
            lines.append("")

        # ── Trade log (last 20) ──────────────────────────────────────
        if self._history:
            records = self._history.get_records()
            recent = records[-20:] if len(records) > 20 else records
            if recent:
                lines.append("RECENT TRADES (last 20)")
                lines.append(f"  {'Time':<20} {'Strategy':<18} {'Side':<5} {'Price':>7} {'Size':>8} {'USD':>8} {'Status':<8}")
                lines.append(f"  {'-' * 80}")

                for rec in reversed(recent):
                    ts = datetime.datetime.fromtimestamp(rec.timestamp).strftime("%m/%d %H:%M:%S")
                    lines.append(
                        f"  {ts:<20} {rec.strategy:<18} {rec.side:<5} "
                        f"${rec.price:>6.3f} {rec.size:>8.1f} ${rec.usd_value:>7.2f} {rec.status:<8}"
                    )
                lines.append("")

        lines.append(f"{'=' * 60}")
        lines.append(f"  End of report")
        lines.append(f"{'=' * 60}")

        report_text = "\n".join(lines)

        try:
            with open(report_path, "w") as f:
                f.write(report_text)
            logger.info("P&L report written to %s", report_path)
        except Exception as exc:
            logger.warning("Failed to write P&L report: %s", exc)

        return report_path

    def _compute_strategy_stats(self) -> Dict[str, StrategyStats]:
        """Compute per-strategy performance from trade history."""
        if not self._history:
            return {}

        records = self._history.get_records()
        stats: Dict[str, StrategyStats] = defaultdict(StrategyStats)

        # Group BUY/SELL pairs by market_id to compute P&L
        buys_by_market: Dict[str, List[TradeRecord]] = defaultdict(list)
        sells_by_market: Dict[str, List[TradeRecord]] = defaultdict(list)

        for rec in records:
            if rec.side == "BUY":
                buys_by_market[rec.market_id].append(rec)
            elif rec.side == "SELL":
                sells_by_market[rec.market_id].append(rec)

            s = stats[rec.strategy]
            s.trades += 1
            s.total_invested += rec.usd_value

        # Match sells to buys for realised P&L
        for market_id, sells in sells_by_market.items():
            buys = buys_by_market.get(market_id, [])
            if not buys:
                continue

            avg_buy_price = sum(b.price * b.size for b in buys) / sum(b.size for b in buys) if buys else 0
            for sell in sells:
                pnl = (sell.price - avg_buy_price) * sell.size
                strategy = sell.strategy

                s = stats[strategy]
                s.total_pnl += pnl
                if pnl > 0:
                    s.wins += 1
                else:
                    s.losses += 1

                if pnl > s.best_trade_pnl:
                    s.best_trade_pnl = pnl
                    s.best_trade_desc = f"{strategy} SELL @ {sell.price:.3f} ({market_id[:16]})"
                if pnl < s.worst_trade_pnl:
                    s.worst_trade_pnl = pnl
                    s.worst_trade_desc = f"{strategy} SELL @ {sell.price:.3f} ({market_id[:16]})"

        return dict(stats)
