from __future__ import annotations

import json
import unittest

from recon_lab.vendor.crashlab.calibration import CalibrationTable, JudgedAnswer
from recon_lab.vendor.crashlab.reliability_stats import wilson_interval


def answers(confidence: float, correct: int, wrong: int) -> list[JudgedAnswer]:
    return [JudgedAnswer(confidence, True)] * correct + [JudgedAnswer(confidence, False)] * wrong


class BinningTests(unittest.TestCase):
    def test_default_is_ten_equal_bins(self) -> None:
        table = CalibrationTable.build([], min_count=1)
        self.assertEqual(len(table["bins"]), 10)
        self.assertEqual(table["bin_edges"][0], 0.0)
        self.assertEqual(table["bin_edges"][-1], 1.0)
        self.assertAlmostEqual(table["bin_edges"][3], 0.3)

    def test_bins_are_half_open_except_the_last(self) -> None:
        edges = CalibrationTable.equal_bins()
        self.assertEqual(CalibrationTable.bin_index(0.0, edges), 0)
        self.assertEqual(CalibrationTable.bin_index(0.3, edges), 3)
        self.assertEqual(CalibrationTable.bin_index(0.2999, edges), 2)
        self.assertEqual(CalibrationTable.bin_index(0.95, edges), 9)
        self.assertEqual(CalibrationTable.bin_index(1.0, edges), 9)

    def test_custom_edges_and_counts(self) -> None:
        data = answers(0.2, 1, 3) + answers(0.55, 2, 2) + answers(0.9, 5, 0)
        table = CalibrationTable.build(data, bin_edges=[0.0, 0.5, 0.8, 1.0], min_count=1)
        self.assertEqual([row["count"] for row in table["bins"]], [4, 4, 5])
        self.assertEqual([row["correct"] for row in table["bins"]], [1, 2, 5])
        self.assertAlmostEqual(table["bins"][1]["mean_confidence"], 0.55)
        self.assertEqual(table["bins"][2]["observed_accuracy"], 1.0)

    def test_invalid_edges_are_rejected_so_no_answer_is_dropped(self) -> None:
        for edges in ([0.0], [0.1, 1.0], [0.0, 0.9], [0.0, 0.5, 0.5, 1.0], []):
            with self.assertRaises(ValueError):
                CalibrationTable.build([], bin_edges=edges)

    def test_judged_answer_validation(self) -> None:
        with self.assertRaises(ValueError):
            JudgedAnswer(1.2, True)
        with self.assertRaises(ValueError):
            JudgedAnswer(float("nan"), True)
        with self.assertRaises(TypeError):
            JudgedAnswer(0.5, 1)  # oracle label must be a real bool
        with self.assertRaises(TypeError):
            JudgedAnswer(True, True)


class MinimumCountTests(unittest.TestCase):
    def test_bins_under_minimum_are_not_measured_and_never_met(self) -> None:
        data = answers(0.75, 3, 1)  # perfectly plausible, but only 4 answers
        table = CalibrationTable.build(data, min_count=10)
        row = table["bins"][7]
        self.assertEqual(row["count"], 4)
        self.assertEqual(row["status"], "not_measured")
        self.assertIsNone(row["observed_accuracy"])
        self.assertIsNone(row["wilson_interval"])
        self.assertIsNone(row["confidence_within_interval"])
        self.assertEqual(table["bins_met"], 0)
        self.assertEqual(table["verdict"], "not_measured")

    def test_one_failing_measured_bin_fails_the_table(self) -> None:
        data = answers(0.75, 8, 2) + answers(0.95, 3, 7)
        table = CalibrationTable.build(data, min_count=10)
        self.assertEqual(table["bins"][7]["status"], "met")
        self.assertEqual(table["bins"][9]["status"], "not_met")
        self.assertEqual(table["verdict"], "not_met")

    def test_all_measured_bins_met(self) -> None:
        table = CalibrationTable.build(answers(0.75, 8, 2), min_count=10)
        self.assertEqual(table["verdict"], "met")
        self.assertEqual(table["bins_not_measured"], 9)


class WilsonParityTests(unittest.TestCase):
    def test_interval_matches_existing_wilson_interval(self) -> None:
        table = CalibrationTable.build(answers(0.65, 13, 7), min_count=5)
        row = table["bins"][6]
        self.assertEqual(row["wilson_interval"], list(wilson_interval(13, 20)))
        low, high = row["wilson_interval"]
        self.assertEqual(row["confidence_within_interval"], low <= 0.65 <= high)

    def test_overconfident_bin_is_outside_interval(self) -> None:
        table = CalibrationTable.build(answers(0.95, 10, 10), min_count=10)
        row = table["bins"][9]
        self.assertFalse(row["confidence_within_interval"])
        self.assertEqual(row["status"], "not_met")


class OutputTests(unittest.TestCase):
    def test_json_and_markdown_outputs(self) -> None:
        table = CalibrationTable.build(answers(0.75, 8, 2) + answers(0.15, 1, 0), min_count=10, missing_count=3)
        parsed = json.loads(CalibrationTable.to_json(table))
        self.assertEqual(parsed["missing_answers"], 3)
        self.assertEqual(parsed["answers"], 11)
        markdown = CalibrationTable.to_markdown(table)
        self.assertIn("| Confidence bin | Count | Mean stated confidence | Observed accuracy | Wilson 95%", markdown)
        self.assertIn("| [0.10, 0.20) | 1 | 0.150 | — | — | — | not measured (count < 10) |", markdown)
        self.assertIn("| [0.90, 1.00] | 0 |", markdown)
        self.assertIn("missing (never counted as correct): 3", markdown)
        self.assertIn("verdict: met", markdown)


if __name__ == "__main__":
    unittest.main()
