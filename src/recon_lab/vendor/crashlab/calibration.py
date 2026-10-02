"""Calibration table: does stated confidence match observed accuracy?

Inputs are typed answers that carry a stated confidence plus an oracle-judged
correct/incorrect label. Answers are binned by confidence. Each bin reports its
count, mean stated confidence, observed accuracy, and a Wilson interval from
`reliability_stats.wilson_interval`, plus whether the mean stated confidence
falls inside that interval.

A bin with fewer than `min_count` answers is "not_measured". It never counts as
met. Missing answers are reported alongside the table and are never counted as
correct.
"""

from __future__ import annotations

import bisect
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .reliability_stats import WILSON_Z, wilson_interval

DEFAULT_BIN_COUNT = 10
DEFAULT_MIN_COUNT = 10

MET = "met"
NOT_MET = "not_met"
NOT_MEASURED = "not_measured"


@dataclass(frozen=True)
class JudgedAnswer:
    confidence: float
    correct: bool

    def __post_init__(self) -> None:
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise TypeError("confidence must be a number")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        if not isinstance(self.correct, bool):
            raise TypeError("correct must be a bool judged by the oracle")


class CalibrationTable:
    @staticmethod
    def equal_bins(count: int = DEFAULT_BIN_COUNT) -> tuple[float, ...]:
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("bin count must be a positive integer")
        return tuple(index / count for index in range(count + 1))

    @staticmethod
    def validate_edges(edges: Sequence[float]) -> tuple[float, ...]:
        values = tuple(float(edge) for edge in edges)
        if len(values) < 2:
            raise ValueError("at least two bin edges are required")
        if values[0] != 0.0 or values[-1] != 1.0:
            raise ValueError("bin edges must start at 0.0 and end at 1.0 so no answer is dropped")
        if any(high <= low for low, high in zip(values, values[1:])):
            raise ValueError("bin edges must be strictly increasing")
        return values

    @staticmethod
    def bin_index(confidence: float, edges: Sequence[float]) -> int:
        """Bins are [low, high) except the last, which is [low, 1.0]."""

        if confidence >= edges[-1]:
            return len(edges) - 2
        return bisect.bisect_right(edges, confidence) - 1

    @staticmethod
    def build(
        answers: Iterable[JudgedAnswer],
        *,
        bin_edges: Sequence[float] | None = None,
        min_count: int = DEFAULT_MIN_COUNT,
        missing_count: int = 0,
        z: float = WILSON_Z,
    ) -> dict[str, Any]:
        if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
            raise ValueError("min_count must be a positive integer")
        if isinstance(missing_count, bool) or not isinstance(missing_count, int) or missing_count < 0:
            raise ValueError("missing_count must be a non-negative integer")
        edges = CalibrationTable.validate_edges(
            CalibrationTable.equal_bins() if bin_edges is None else bin_edges
        )
        items = list(answers)
        for item in items:
            if not isinstance(item, JudgedAnswer):
                raise TypeError("answers must be JudgedAnswer instances")

        grouped: list[list[JudgedAnswer]] = [[] for _ in range(len(edges) - 1)]
        for item in items:
            grouped[CalibrationTable.bin_index(item.confidence, edges)].append(item)

        bins = [
            CalibrationTable._bin_row(edges[i], edges[i + 1], grouped[i], min_count, z)
            for i in range(len(grouped))
        ]
        statuses = [row["status"] for row in bins]
        if NOT_MET in statuses:
            verdict = NOT_MET
        elif MET in statuses:
            verdict = MET
        else:
            verdict = NOT_MEASURED
        return {
            "schema_version": "calibration-table-v1",
            "z": z,
            "min_count": min_count,
            "bin_edges": list(edges),
            "answers": len(items),
            "missing_answers": missing_count,
            "bins": bins,
            "bins_met": statuses.count(MET),
            "bins_not_met": statuses.count(NOT_MET),
            "bins_not_measured": statuses.count(NOT_MEASURED),
            "verdict": verdict,
        }

    @staticmethod
    def _bin_row(
        low: float,
        high: float,
        items: list[JudgedAnswer],
        min_count: int,
        z: float,
    ) -> dict[str, Any]:
        count = len(items)
        correct = sum(1 for item in items if item.correct)
        mean_confidence = sum(item.confidence for item in items) / count if count else None
        row: dict[str, Any] = {
            "lower": low,
            "upper": high,
            "upper_inclusive": high == 1.0,
            "count": count,
            "correct": correct,
            "mean_confidence": mean_confidence,
            "observed_accuracy": None,
            "wilson_interval": None,
            "confidence_within_interval": None,
            "status": NOT_MEASURED,
        }
        if count < min_count:
            return row
        interval_low, interval_high = wilson_interval(correct, count, z)
        within = interval_low <= mean_confidence <= interval_high
        row.update(
            observed_accuracy=correct / count,
            wilson_interval=[interval_low, interval_high],
            confidence_within_interval=within,
            status=MET if within else NOT_MET,
        )
        return row

    @staticmethod
    def to_json(table: dict[str, Any]) -> str:
        return json.dumps(table, indent=2, sort_keys=True, allow_nan=False)

    @staticmethod
    def to_markdown(table: dict[str, Any]) -> str:
        level = "95%" if table["z"] == WILSON_Z else f"z={table['z']:g}"
        lines = [
            f"| Confidence bin | Count | Mean stated confidence | Observed accuracy | Wilson {level} | Stated inside interval | Status |",
            "| --- | ---: | ---: | ---: | --- | --- | --- |",
        ]
        for row in table["bins"]:
            closing = "]" if row["upper_inclusive"] else ")"
            label = f"[{row['lower']:.2f}, {row['upper']:.2f}{closing}"
            mean = "—" if row["mean_confidence"] is None else f"{row['mean_confidence']:.3f}"
            if row["status"] == NOT_MEASURED:
                accuracy = interval = inside = "—"
                status = f"not measured (count < {table['min_count']})"
            else:
                accuracy = f"{row['observed_accuracy']:.3f}"
                interval = f"[{row['wilson_interval'][0]:.3f}, {row['wilson_interval'][1]:.3f}]"
                inside = "yes" if row["confidence_within_interval"] else "no"
                status = "met" if row["status"] == MET else "not met"
            lines.append(f"| {label} | {row['count']} | {mean} | {accuracy} | {interval} | {inside} | {status} |")
        lines.append("")
        lines.append(
            f"Answers binned: {table['answers']} · missing (never counted as correct): "
            f"{table['missing_answers']} · minimum per bin: {table['min_count']} · "
            f"verdict: {table['verdict'].replace('_', ' ')}"
        )
        return "\n".join(lines) + "\n"
