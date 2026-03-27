"""
ai_calibration.py
-----------------
Tracks AI probability predictions vs actual market outcomes to measure
and improve calibration over time.

Stores prediction records to a CSV file. Computes calibration metrics
(Brier score, calibration curve bins) and adjusts AI confidence based on
historical accuracy.

Usage:
    calibrator = AICalibrator()
    calibrator.record_prediction(market_id, predicted_prob, outcome_token)
    calibrator.record_outcome(market_id, actual_result)  # True/False
    adjusted = calibrator.adjust_confidence(raw_confidence, category)
"""

import csv
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("bot.ai_calibration")

CALIBRATION_FILE = "ai_calibration.csv"

# Number of bins for calibration curve
N_BINS = 10


@dataclass
class PredictionRecord:
    market_id: str
    predicted_prob: float
    category: str
    timestamp: float
    actual_outcome: Optional[bool] = None  # None = not yet resolved
    resolved_at: Optional[float] = None


class AICalibrator:
    """Tracks and improves AI prediction calibration."""

    def __init__(self, data_dir: str = ".") -> None:
        self._file = os.path.join(data_dir, CALIBRATION_FILE)
        self._records: Dict[str, PredictionRecord] = {}
        self._category_adjustments: Dict[str, float] = {}
        self._load()

    def record_prediction(
        self, market_id: str, predicted_prob: float, category: str = "general"
    ) -> None:
        """Record a new AI prediction."""
        rec = PredictionRecord(
            market_id=market_id,
            predicted_prob=predicted_prob,
            category=category,
            timestamp=time.time(),
        )
        self._records[market_id] = rec
        self._save_record(rec)
        logger.debug(
            "Calibration: recorded prediction for %s: %.2f (%s)",
            market_id[:16], predicted_prob, category,
        )

    def record_outcome(self, market_id: str, won: bool) -> None:
        """Record the actual outcome for a previously predicted market."""
        rec = self._records.get(market_id)
        if rec is None:
            return
        rec.actual_outcome = won
        rec.resolved_at = time.time()
        self._recompute_adjustments()
        logger.info(
            "Calibration: %s resolved=%s (predicted=%.2f)",
            market_id[:16], won, rec.predicted_prob,
        )

    def adjust_confidence(self, raw_confidence: float, category: str = "general") -> float:
        """
        Adjust a raw AI confidence score based on historical calibration.

        If AI historically overestimates in a category, this nudges down.
        If it underestimates, this nudges up.
        """
        adjustment = self._category_adjustments.get(category, 0.0)
        # Also check "general" as fallback
        if adjustment == 0.0 and category != "general":
            adjustment = self._category_adjustments.get("general", 0.0)
        adjusted = raw_confidence + adjustment
        return max(0.05, min(0.95, adjusted))

    def brier_score(self) -> Optional[float]:
        """
        Compute Brier score across all resolved predictions.
        Lower is better. 0.0 = perfect, 0.25 = random.
        """
        resolved = [r for r in self._records.values() if r.actual_outcome is not None]
        if len(resolved) < 5:
            return None
        total = sum(
            (r.predicted_prob - (1.0 if r.actual_outcome else 0.0)) ** 2
            for r in resolved
        )
        return total / len(resolved)

    def calibration_summary(self) -> str:
        """Return a human-readable calibration summary."""
        resolved = [r for r in self._records.values() if r.actual_outcome is not None]
        if not resolved:
            return "No resolved predictions yet."
        brier = self.brier_score()
        lines = [f"Resolved predictions: {len(resolved)}"]
        if brier is not None:
            lines.append(f"Brier score: {brier:.4f} (lower is better, 0.25 = random)")
        # Calibration bins
        bins: Dict[int, List[bool]] = {i: [] for i in range(N_BINS)}
        for r in resolved:
            bin_idx = min(int(r.predicted_prob * N_BINS), N_BINS - 1)
            bins[bin_idx].append(r.actual_outcome)
        lines.append("Calibration curve:")
        for i in range(N_BINS):
            if bins[i]:
                actual_rate = sum(bins[i]) / len(bins[i])
                expected = (i + 0.5) / N_BINS
                lines.append(
                    f"  {i*10}-{(i+1)*10}%: predicted={expected:.0%} actual={actual_rate:.0%} (n={len(bins[i])})"
                )
        return "\n".join(lines)

    def _recompute_adjustments(self) -> None:
        """Recompute per-category calibration adjustments."""
        resolved = [r for r in self._records.values() if r.actual_outcome is not None]
        if len(resolved) < 10:
            return

        # Group by category
        by_cat: Dict[str, List[PredictionRecord]] = {}
        for r in resolved:
            by_cat.setdefault(r.category, []).append(r)
            by_cat.setdefault("general", []).append(r)

        for cat, recs in by_cat.items():
            if len(recs) < 5:
                continue
            # Average predicted vs average actual
            avg_predicted = sum(r.predicted_prob for r in recs) / len(recs)
            avg_actual = sum(1.0 if r.actual_outcome else 0.0 for r in recs) / len(recs)
            # If we predict 70% on average but only 60% happen,
            # adjustment = -0.10 (nudge predictions down)
            raw_adj = avg_actual - avg_predicted
            # Dampen the adjustment (don't overreact)
            self._category_adjustments[cat] = raw_adj * 0.5

    def _load(self) -> None:
        """Load existing calibration data from CSV."""
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rec = PredictionRecord(
                        market_id=row["market_id"],
                        predicted_prob=float(row["predicted_prob"]),
                        category=row.get("category", "general"),
                        timestamp=float(row.get("timestamp", 0)),
                    )
                    outcome = row.get("actual_outcome", "")
                    if outcome == "True":
                        rec.actual_outcome = True
                        rec.resolved_at = float(row.get("resolved_at", 0))
                    elif outcome == "False":
                        rec.actual_outcome = False
                        rec.resolved_at = float(row.get("resolved_at", 0))
                    self._records[rec.market_id] = rec
            self._recompute_adjustments()
            logger.info("Loaded %d calibration records.", len(self._records))
        except Exception as exc:
            logger.warning("Failed to load calibration data: %s", exc)

    def _save_record(self, rec: PredictionRecord) -> None:
        """Append a single record to CSV."""
        file_exists = os.path.exists(self._file)
        try:
            with open(self._file, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "market_id", "predicted_prob", "category", "timestamp",
                    "actual_outcome", "resolved_at",
                ])
                if not file_exists:
                    writer.writeheader()
                writer.writerow({
                    "market_id": rec.market_id,
                    "predicted_prob": f"{rec.predicted_prob:.4f}",
                    "category": rec.category,
                    "timestamp": f"{rec.timestamp:.0f}",
                    "actual_outcome": str(rec.actual_outcome) if rec.actual_outcome is not None else "",
                    "resolved_at": f"{rec.resolved_at:.0f}" if rec.resolved_at else "",
                })
        except Exception as exc:
            logger.debug("Failed to save calibration record: %s", exc)
