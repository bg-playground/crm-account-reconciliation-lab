import copy
import json
import unittest
from pathlib import Path

from recon_lab.evaluate import BarChecker, CalibrationSummary, PairMetrics
from recon_lab.report import ResultsPage

RULES = json.loads((Path(__file__).resolve().parents[1] / "config" / "protocol_rules.json").read_text())
BAR = RULES["bar"]


def rate(s, n):
    return PairMetrics.rate(s, n)


def passing_metrics():
    calib = CalibrationSummary.build([(0.95, True)] * 95 + [(0.95, False)] * 5 + [(0.05, False)] * 95 + [(0.05, True)] * 5,
                                     missing=0, min_count=30)
    return {
        "blocking_recall": rate(99, 100), "candidates": 2000, "true_pairs": 100, "review_queue_size": 50,
        "cascade": {"precision": rate(990, 1000), "recall": rate(90, 100)},
        "baseline": {"precision": rate(70, 70), "recall": rate(70, 100), "review_size": 200, "reachable_recall": 0.95},
        "cascade_reachable_recall": 0.97, "calibration": calib,
        "answers": {"missing_rate": 0.01, "version_coverage": 1.0, "model_log_run_valid": True},
        "unseen_family_recall": rate(8, 10), "published_spend_usd": 0.5,
    }


class PairMetricsTests(unittest.TestCase):
    def test_rate_and_wilson(self):
        r = rate(2, 20)
        self.assertAlmostEqual(r["value"], 0.1)
        self.assertEqual(round(r["wilson_low"], 3), 0.028)
        self.assertIsNone(rate(0, 0)["value"])

    def test_confusion(self):
        c = PairMetrics.confusion({("a", "1"), ("a", "2")}, {("a", "1"), ("a", "3")})
        self.assertEqual(c["true_positives"], 1)
        self.assertEqual(c["false_positives"], 1)
        self.assertEqual(c["precision"]["value"], 0.5)
        self.assertEqual(c["recall"]["value"], 0.5)


class CalibrationSummaryTests(unittest.TestCase):
    def test_well_calibrated(self):
        s = passing_metrics()["calibration"]
        self.assertEqual(s["measured_bins"], 2)
        self.assertEqual(s["met_bins"], 2)
        self.assertAlmostEqual(s["max_abs_gap"], 0.0)

    def test_nothing_measured(self):
        s = CalibrationSummary.build([(0.5, True)] * 5, missing=0, min_count=30)
        self.assertEqual(s["measured_bins"], 0)
        self.assertIsNone(s["met_share"])


class BarCheckerTests(unittest.TestCase):
    def test_pass(self):
        v = BarChecker.check(passing_metrics(), BAR)
        self.assertEqual(v["verdict"], "PASS", v["failed"])
        self.assertEqual(len(v["criteria"]), 9)

    def test_precision_wilson_low_fails_and_invalidates(self):
        m = passing_metrics()
        m["cascade"]["precision"] = rate(49, 50)  # 0.98 but Wilson low ~0.895
        v = BarChecker.check(m, BAR)
        self.assertEqual(v["verdict"], "KILL")
        self.assertTrue(v["run_invalid"])
        self.assertIn("2_auto_merge_precision", v["failed"])

    def test_not_measured_calibration_fails(self):
        m = passing_metrics()
        m["calibration"] = CalibrationSummary.build([(0.5, True)] * 5, missing=0, min_count=30)
        v = BarChecker.check(m, BAR)
        self.assertIn("6_calibration", v["failed"])
        self.assertFalse(v["run_invalid"])

    def test_baseline_comparison_either_branch(self):
        m = passing_metrics()
        m["cascade"]["recall"] = rate(86, 100)
        m["baseline"]["recall"] = rate(84, 100)  # gain 0.02 < 0.05
        m["review_queue_size"] = 150  # > 0.7 * 200
        self.assertIn("5_beats_baseline", BarChecker.check(m, BAR)["failed"])
        m["review_queue_size"] = 140  # == 0.7 * 200, reachable 0.97 >= 0.95 - 0.01
        self.assertNotIn("5_beats_baseline", BarChecker.check(m, BAR)["failed"])

    def test_review_queue_limits(self):
        m = passing_metrics()
        m["review_queue_size"] = 301
        self.assertIn("4_review_queue", BarChecker.check(m, BAR)["failed"])

    def test_missing_rate_and_versions(self):
        m = passing_metrics()
        m["answers"] = {"missing_rate": 0.03, "version_coverage": 1.0, "model_log_run_valid": True}
        self.assertIn("7_answers_and_versions", BarChecker.check(m, BAR)["failed"])
        m["answers"] = {"missing_rate": 0.0, "version_coverage": 0.99, "model_log_run_valid": True}
        self.assertIn("7_answers_and_versions", BarChecker.check(m, BAR)["failed"])

    def test_bar_is_not_mutated(self):
        bar = copy.deepcopy(BAR)
        BarChecker.check(passing_metrics(), bar)
        self.assertEqual(bar, BAR)


class ResultsPageTests(unittest.TestCase):
    def test_renders(self):
        m = passing_metrics()
        m.update({"answers": {**m["answers"], "missing": 1, "calls": 100, "returned_models": {"x-1": 99}},
                  "cascade_decisions": {"merge_ai": 3}, "review_queue_true_pairs": 2,
                  "unseen_family_recall_baseline": rate(1, 10), "disguised_pairs": 300,
                  "disguised_names": {"true_pairs_in_sample": 10, "recall_main_repeat1": 0.9, "recall_disguised": 0.8, "flagged": False},
                  "tokens": {"input": 1, "output": 1, "reasoning": 0}, "family_recall": {"web_missing": {"cascade": rate(1, 2), "baseline": rate(0, 2)}}})
        out = {"metrics": m, "verdict": BarChecker.check(m, BAR), "cutoffs": {"splink_merge_weight": 20.0, "ai_merge": 0.9, "ai_nonmatch": 0.1},
               "run_id": "r", "freeze_commit": "abc", "prompt_sha256": "f" * 64}
        page = ResultsPage.render(out, {"rules": RULES})
        self.assertIn("Verdict against the frozen bar: PASS", page)
        self.assertIn("synthetic", page)


if __name__ == "__main__":
    unittest.main()
