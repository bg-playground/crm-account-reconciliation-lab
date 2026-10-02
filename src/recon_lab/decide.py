"""Cutoff selection on dev, and the frozen Splink -> AI cascade."""

from __future__ import annotations

import math
from collections.abc import Iterable

from .blocking import Pair
from .vendor.crashlab.run_mode import ACCEPT, MISSING, DecisionLedger, ThresholdCheck
from .vendor.crashlab.typed_answers import MissingAnswer, MissingReason, ParsedAnswer, TypedAnswer

# Splink probability 0.01 expressed as a match weight (log2 Bayes factor, prior folded in).
BASELINE_REVIEW_FLOOR_MW = math.log2(0.01 / 0.99)
MERGE_SPLINK = "merge_splink"
MERGE_AI = "merge_ai"
NOMATCH_AI = "nomatch_ai"
REVIEW_AI = "review_ai_uncertain"
REVIEW_MISSING = "review_ai_missing"
REVIEW_NOT_JUDGED = "review_not_judged"
MERGES = (MERGE_SPLINK, MERGE_AI)
REVIEWS = (REVIEW_AI, REVIEW_MISSING, REVIEW_NOT_JUDGED)


class CutoffSelector:
    @staticmethod
    def precision(predicted: set[Pair], truth: set[Pair]) -> float | None:
        return len(predicted & truth) / len(predicted) if predicted else None

    @staticmethod
    def splink_merge_weight(weights: dict[Pair, float], truth: set[Pair], grid: Iterable[float],
                            target: float) -> float | None:
        """Lowest match-weight cutoff whose dev auto-merge precision >= target (None = no Splink auto-merge)."""

        for cutoff in sorted(grid):
            prec = CutoffSelector.precision({p for p, w in weights.items() if w >= cutoff}, truth)
            if prec is not None and prec >= target:
                return float(cutoff)
        return None

    @staticmethod
    def ai_merge(ai_p: dict[Pair, float], splink_merged: set[Pair], truth: set[Pair], grid: Iterable[float],
                 target: float) -> float | None:
        """Lowest AI cutoff whose dev cascade auto-merge precision (Splink merges + AI merges) >= target."""

        for cutoff in sorted(grid):
            merged = splink_merged | {p for p, v in ai_p.items() if v >= cutoff}
            prec = CutoffSelector.precision(merged, truth)
            if prec is not None and prec >= target:
                return float(cutoff)
        return None

    @staticmethod
    def ai_nonmatch(ai_p: dict[Pair, float], truth: set[Pair], total_true: int, grid: Iterable[float],
                    max_loss_share: float) -> float | None:
        """Highest AI no-match cutoff that discards at most max_loss_share of all dev true pairs."""

        for cutoff in sorted(grid, reverse=True):
            lost = sum(1 for p, v in ai_p.items() if v <= cutoff and p in truth)
            if lost <= max_loss_share * total_true:
                return float(cutoff)
        return None


class Cascade:
    """Splink auto-merges at or above its frozen weight; every other candidate goes to the AI judge."""

    @staticmethod
    def checks(c_ai: float, l_ai: float) -> list[ThresholdCheck]:
        return [ThresholdCheck("ai_merge", "value", ">=", c_ai), ThresholdCheck("ai_nonmatch", "value", "<=", l_ai)]

    @staticmethod
    def mean_answer(probabilities: list[float | None]) -> TypedAnswer:
        values = [p for p in probabilities if p is not None]
        if not values:
            return MissingAnswer("same_organization", "yes_no", MissingReason.MISSING_FIELD, "no repeat returned a typed answer")
        mean = sum(values) / len(values)
        return ParsedAnswer("same_organization", "yes_no", mean, max(mean, 1 - mean))

    @staticmethod
    def ai_outcome(ledger: DecisionLedger, pair_id: str, answer: TypedAnswer) -> str:
        merge = ledger.decide(pair_id, "ai_merge", answer)
        if merge.outcome == MISSING:
            return REVIEW_MISSING
        if merge.outcome == ACCEPT:
            return MERGE_AI
        if ledger.decide(pair_id, "ai_nonmatch", answer).outcome == ACCEPT:
            return NOMATCH_AI
        return REVIEW_AI

    @staticmethod
    def baseline_decisions(weights: dict[Pair, float], ts: float | None) -> dict[Pair, str]:
        out = {}
        for pair, weight in weights.items():
            if ts is not None and weight >= ts:
                out[pair] = "merge"
            elif weight >= BASELINE_REVIEW_FLOOR_MW:
                out[pair] = "review"
            else:
                out[pair] = "nomatch"
        return out

