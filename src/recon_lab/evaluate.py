"""Metrics with Wilson bounds, calibration summary, and the frozen pass bar."""

from __future__ import annotations

from typing import Any

from .blocking import Pair
from .vendor.crashlab.calibration import CalibrationTable, JudgedAnswer
from .vendor.crashlab.reliability_stats import wilson_interval

INVALIDATING = ("1_blocking_recall", "2_auto_merge_precision", "7_answers_and_versions", "9_published_spend")


class PairMetrics:
    @staticmethod
    def rate(successes: int, trials: int) -> dict[str, Any]:
        if trials == 0:
            return {"value": None, "successes": 0, "trials": 0, "wilson_low": None, "wilson_high": None}
        low, high = wilson_interval(successes, trials)
        return {"value": successes / trials, "successes": successes, "trials": trials,
                "wilson_low": low, "wilson_high": high}

    @staticmethod
    def confusion(predicted: set[Pair], truth: set[Pair]) -> dict[str, Any]:
        tp = len(predicted & truth)
        return {"predicted": len(predicted), "true_positives": tp, "false_positives": len(predicted) - tp,
                "precision": PairMetrics.rate(tp, len(predicted)), "recall": PairMetrics.rate(tp, len(truth))}


class CalibrationSummary:
    @staticmethod
    def build(pairs_p: list[tuple[float, bool]], missing: int, min_count: int) -> dict[str, Any]:
        table = CalibrationTable.build([JudgedAnswer(p, truth) for p, truth in pairs_p],
                                       min_count=min_count, missing_count=missing)
        measured = [b for b in table["bins"] if b["status"] != "not_measured"]
        met = [b for b in measured if b["status"] == "met"]
        gaps = [abs(b["mean_confidence"] - b["observed_accuracy"]) for b in measured]
        return {"table": table, "measured_bins": len(measured), "met_bins": len(met),
                "met_share": (len(met) / len(measured)) if measured else None,
                "max_abs_gap": max(gaps) if gaps else None}


class BarChecker:
    """Checks results against the frozen bar. Never mutates the bar."""

    @staticmethod
    def _row(name: str, passed: bool, observed: Any, required: str) -> dict[str, Any]:
        return {"criterion": name, "passed": bool(passed), "observed": observed, "required": required}

    @staticmethod
    def check(m: dict[str, Any], bar: dict[str, Any]) -> dict[str, Any]:
        rows = []
        rows.append(BarChecker._row("1_blocking_recall", m["blocking_recall"]["value"] >= bar["blocking_recall_min"],
                                    m["blocking_recall"]["value"], f">= {bar['blocking_recall_min']}"))
        prec = m["cascade"]["precision"]
        ok2 = (prec["value"] is not None and prec["value"] >= bar["auto_merge_precision_min"]
               and prec["wilson_low"] >= bar["auto_merge_precision_wilson_low_min"])
        rows.append(BarChecker._row("2_auto_merge_precision", ok2, {"precision": prec["value"], "wilson_low": prec["wilson_low"]},
                                    f">= {bar['auto_merge_precision_min']} and Wilson low >= {bar['auto_merge_precision_wilson_low_min']}"))
        rec = m["cascade"]["recall"]["value"]
        rows.append(BarChecker._row("3_auto_merge_recall", rec >= bar["auto_merge_recall_min"], rec,
                                    f">= {bar['auto_merge_recall_min']}"))
        rq, cand = m["review_queue_size"], m["candidates"]
        ok4 = rq <= bar["review_share_max"] * cand and rq <= bar["review_pairs_max"]
        rows.append(BarChecker._row("4_review_queue", ok4, {"pairs": rq, "share": rq / cand if cand else None},
                                    f"<= {bar['review_share_max']:.0%} of candidates and <= {bar['review_pairs_max']} pairs"))
        b = m["baseline"]
        recall_gain = rec - b["recall"]["value"]
        review_ok = m["review_queue_size"] <= bar["review_reduction_factor"] * b["review_size"]
        reach_ok = m["cascade_reachable_recall"] >= b["reachable_recall"] - bar["reachable_recall_tolerance"]
        ok5 = recall_gain >= bar["recall_gain_min"] or (review_ok and reach_ok)
        rows.append(BarChecker._row("5_beats_baseline", ok5,
                                    {"recall_gain": recall_gain, "cascade_review": m["review_queue_size"],
                                     "baseline_review": b["review_size"], "cascade_reachable": m["cascade_reachable_recall"],
                                     "baseline_reachable": b["reachable_recall"]},
                                    f"recall gain >= {bar['recall_gain_min']} OR (review <= {bar['review_reduction_factor']} x baseline review AND reachable recall >= baseline - {bar['reachable_recall_tolerance']})"))
        cal = m["calibration"]
        ok6 = (cal["measured_bins"] > 0 and cal["met_share"] >= bar["calibration_met_share_min"]
               and cal["max_abs_gap"] <= bar["calibration_max_gap"])
        rows.append(BarChecker._row("6_calibration", ok6, {"measured_bins": cal["measured_bins"], "met_share": cal["met_share"],
                                    "max_abs_gap": cal["max_abs_gap"]},
                                    f">= {bar['calibration_met_share_min']:.0%} of measured bins met and max gap <= {bar['calibration_max_gap']} (no measured bins = fail)"))
        ans = m["answers"]
        ok7 = (ans["missing_rate"] <= bar["missing_rate_max"] and ans["version_coverage"] >= 1.0 and ans["model_log_run_valid"])
        rows.append(BarChecker._row("7_answers_and_versions", ok7, {"missing_rate": ans["missing_rate"],
                                    "version_coverage": ans["version_coverage"], "run_valid": ans["model_log_run_valid"]},
                                    f"missing <= {bar['missing_rate_max']:.0%}, 100% returned versions, model log run_valid"))
        unseen = m["unseen_family_recall"]["value"]
        rows.append(BarChecker._row("8_unseen_family_recall", unseen is not None and unseen >= bar["unseen_family_recall_min"],
                                    unseen, f">= {bar['unseen_family_recall_min']}"))
        rows.append(BarChecker._row("9_published_spend", m["published_spend_usd"] <= bar["published_spend_max_usd"],
                                    m["published_spend_usd"], f"<= ${bar['published_spend_max_usd']}"))
        failed = [r["criterion"] for r in rows if not r["passed"]]
        invalid = [c for c in failed if c in INVALIDATING]
        verdict = "PASS" if not failed else "KILL"
        return {"verdict": verdict, "run_invalid": bool(invalid), "invalidating_failures": invalid,
                "failed": failed, "criteria": rows}
