"""
trade_history.py
----------------
Records every executed trade to a persistent CSV file and provides
performance analytics across paper, micro, and live modes.

CSV columns:
    timestamp, strategy, market_id, token_id, side, price, size, usd_value,
    order_type, mode, order_id, status, reason

The TradeHistory is loaded from disk on startup to maintain history across
bot restarts.
"""

import csv
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from execution import ExecutionResult

logger = logging.getLogger("bot.trade_history")

# Default path for the CSV trade log
DEFAULT_HISTORY_FILE = "trade_history.csv"

# CSV column order
CSV_COLUMNS = [
    "timestamp",
    "strategy",
    "market_id",
    "token_id",
    "side",
    "price",
    "size",
    "usd_value",
    "order_type",
    "mode",
    "order_id",
    "status",
    "reason",
]


@dataclass
class TradeRecord:
    """A single trade entry stored in the history CSV."""

    timestamp: float
    strategy: str
    market_id: str
    token_id: str
    side: str         # "BUY" or "SELL"
    price: float
    size: float
    usd_value: float
    order_type: str   # "GTC" or "FOK"
    mode: str         # "paper", "micro", or "live"
    order_id: str
    status: str       # "paper", "micro", "submitted", "error", etc.
    reason: str

    def to_csv_row(self) -> dict:
        """Return a dict suitable for csv.DictWriter."""
        return {
            "timestamp": f"{self.timestamp:.3f}",
            "strategy":  self.strategy,
            "market_id": self.market_id,
            "token_id":  self.token_id,
            "side":      self.side,
            "price":     f"{self.price:.6f}",
            "size":      f"{self.size:.6f}",
            "usd_value": f"{self.usd_value:.4f}",
            "order_type": self.order_type,
            "mode":      self.mode,
            "order_id":  self.order_id,
            "status":    self.status,
            "reason":    self.reason.replace("\n", " "),
        }


class TradeHistory:
    """
    Persistent record of all trades executed by the bot.

    Loads existing history from CSV on startup and appends new records
    in real-time so history survives bot restarts.

    Usage:
        history = TradeHistory()
        history.record_trade(execution_result)
        history.print_report()
    """

    def __init__(self, history_file: str = DEFAULT_HISTORY_FILE) -> None:
        self.history_file = history_file
        self._records: List[TradeRecord] = []
        self._load()

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def record_trade(self, result: ExecutionResult) -> None:
        """
        Append an executed trade to the history.

        Only records successful executions (success=True).  Failed/rejected
        orders are not added to the permanent history.

        Args:
            result: ExecutionResult from Executor.execute().
        """
        if not result.success:
            return

        signal = result.signal
        record = TradeRecord(
            timestamp=result.timestamp,
            strategy=signal.strategy,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=signal.side,
            price=result.filled_price if result.filled_price is not None else signal.price,
            size=result.filled_size if result.filled_size is not None else signal.size,
            usd_value=(result.filled_price or signal.price) * (result.filled_size or signal.size),
            order_type=signal.order_type,
            mode=result.mode,
            order_id=result.order_id or "",
            status=result.status,
            reason=signal.reason,
        )

        self._records.append(record)
        self._append_to_csv(record)

        logger.debug(
            "Trade recorded: %s %s %.4f shares @ $%.4f [%s]",
            record.side,
            record.token_id[:16],
            record.size,
            record.price,
            record.mode,
        )

    def get_summary(self) -> dict:
        """
        Compute a performance summary across all recorded trades.

        Returns:
            Dict with keys:
              - total_trades: int
              - by_mode: {mode: count}
              - by_strategy: {strategy: {trades, buys, sells, total_usd}}
              - total_usd_volume: float
              - buy_count: int
              - sell_count: int
        """
        summary: dict = {
            "total_trades": len(self._records),
            "by_mode": {},
            "by_strategy": {},
            "total_usd_volume": 0.0,
            "buy_count": 0,
            "sell_count": 0,
        }

        for rec in self._records:
            # Mode breakdown
            summary["by_mode"][rec.mode] = summary["by_mode"].get(rec.mode, 0) + 1

            # Strategy breakdown
            strat = rec.strategy
            if strat not in summary["by_strategy"]:
                summary["by_strategy"][strat] = {
                    "trades": 0,
                    "buys": 0,
                    "sells": 0,
                    "total_usd": 0.0,
                }
            s = summary["by_strategy"][strat]
            s["trades"] += 1
            s["total_usd"] += rec.usd_value
            if rec.side == "BUY":
                s["buys"] += 1
                summary["buy_count"] += 1
            else:
                s["sells"] += 1
                summary["sell_count"] += 1

            summary["total_usd_volume"] += rec.usd_value

        return summary

    def print_report(self) -> None:
        """
        Print a formatted performance summary to the console.

        Intended to be called on bot shutdown to give a final recap of all
        activity during the session (and all previous sessions).
        """
        summary = self.get_summary()
        sep = "=" * 60

        print(sep)
        print("  TRADE HISTORY REPORT")
        print(sep)
        print(f"  History file : {self.history_file}")
        print(f"  Total trades : {summary['total_trades']}")
        print(f"  BUYs         : {summary['buy_count']}")
        print(f"  SELLs        : {summary['sell_count']}")
        print(f"  Volume (USD) : ${summary['total_usd_volume']:.2f}")
        print()

        if summary["by_mode"]:
            print("  By Mode:")
            for mode, count in sorted(summary["by_mode"].items()):
                print(f"    {mode:<10} {count:>5} trades")
            print()

        if summary["by_strategy"]:
            print("  By Strategy:")
            print(f"    {'Strategy':<20} {'Trades':>7} {'BUYs':>6} {'SELLs':>7} {'Volume':>12}")
            print(f"    {'-'*20} {'-'*7} {'-'*6} {'-'*7} {'-'*12}")
            for strat, data in sorted(summary["by_strategy"].items()):
                print(
                    f"    {strat:<20} {data['trades']:>7} "
                    f"{data['buys']:>6} {data['sells']:>7} "
                    f"${data['total_usd']:>11.2f}"
                )
            print()

        print(sep)

    def compute_log_returns(self) -> Dict[str, float]:
        """
        Compute log returns for all completed round-trip trades.

        Log return = ln(P_sell / P_buy)

        Log returns sum correctly across positions and time periods,
        making them preferable for multi-position P&L analysis.

        Returns:
            Dict with keys: total_log_return, avg_log_return, count,
            arithmetic_return (for display).
        """
        by_token: Dict[str, List[TradeRecord]] = {}
        for rec in self._records:
            by_token.setdefault(rec.token_id, []).append(rec)

        log_returns = []
        arith_returns = []

        for token_id, records in by_token.items():
            buys = sorted([r for r in records if r.side == "BUY"], key=lambda r: r.timestamp)
            sells = sorted([r for r in records if r.side == "SELL"], key=lambda r: r.timestamp)

            for buy in buys:
                # Find first sell after this buy
                matched_sell = None
                for sell in sells:
                    if sell.timestamp > buy.timestamp:
                        matched_sell = sell
                        break

                if matched_sell and buy.price > 0 and matched_sell.price > 0:
                    log_r = math.log(matched_sell.price / buy.price)
                    arith_r = (matched_sell.price - buy.price) / buy.price
                    log_returns.append(log_r)
                    arith_returns.append(arith_r)
                    sells.remove(matched_sell)

        total_log = sum(log_returns) if log_returns else 0.0
        avg_log = total_log / len(log_returns) if log_returns else 0.0
        total_arith = sum(arith_returns) if arith_returns else 0.0

        return {
            "total_log_return": total_log,
            "avg_log_return": avg_log,
            "count": len(log_returns),
            "arithmetic_return": total_arith,
            "avg_arithmetic_return": total_arith / len(arith_returns) if arith_returns else 0.0,
        }

    def get_records(self, mode: Optional[str] = None, strategy: Optional[str] = None) -> List[TradeRecord]:
        """
        Return trade records optionally filtered by mode and/or strategy.

        Args:
            mode:     Filter to "paper", "micro", or "live" (None = all).
            strategy: Filter to a specific strategy name (None = all).

        Returns:
            Filtered list of TradeRecord objects, oldest first.
        """
        records = self._records
        if mode:
            records = [r for r in records if r.mode == mode]
        if strategy:
            records = [r for r in records if r.strategy == strategy]
        return records

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load existing trade history from the CSV file on startup."""
        if not os.path.exists(self.history_file):
            logger.info("No existing trade history file at %s; starting fresh.", self.history_file)
            return

        try:
            loaded = 0
            with open(self.history_file, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        record = TradeRecord(
                            timestamp=float(row.get("timestamp", 0)),
                            strategy=row.get("strategy", ""),
                            market_id=row.get("market_id", ""),
                            token_id=row.get("token_id", ""),
                            side=row.get("side", ""),
                            price=float(row.get("price", 0)),
                            size=float(row.get("size", 0)),
                            usd_value=float(row.get("usd_value", 0)),
                            order_type=row.get("order_type", "GTC"),
                            mode=row.get("mode", "paper"),
                            order_id=row.get("order_id", ""),
                            status=row.get("status", ""),
                            reason=row.get("reason", ""),
                        )
                        self._records.append(record)
                        loaded += 1
                    except Exception as exc:
                        logger.debug("Skipping malformed history row: %s", exc)

            logger.info(
                "Loaded %d trade records from %s.", loaded, self.history_file
            )
        except Exception as exc:
            logger.error("Failed to load trade history: %s", exc)

    def _append_to_csv(self, record: TradeRecord) -> None:
        """Append a single trade record to the CSV file."""
        file_exists = os.path.exists(self.history_file)
        try:
            with open(self.history_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                if not file_exists or os.path.getsize(self.history_file) == 0:
                    writer.writeheader()
                writer.writerow(record.to_csv_row())
        except Exception as exc:
            logger.error("Failed to write trade to history CSV: %s", exc)
