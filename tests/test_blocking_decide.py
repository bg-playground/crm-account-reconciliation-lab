import math
import tempfile
import unittest
from pathlib import Path

from recon_lab.blocking import Blocker
from recon_lab.decide import (BASELINE_REVIEW_FLOOR_MW, MERGE_AI, NOMATCH_AI, REVIEW_AI, REVIEW_MISSING, Cascade,
                              CutoffSelector)
from recon_lab.vendor.crashlab.run_mode import DecisionLedger


def rec(uid, **kw):
    base = {"unique_id": uid, "domain_norm": None, "name4": None, "postcode5": None, "phone10": None,
            "soundex_first": None, "state_norm": None}
    base.update(kw)
    return base


class BlockerTests(unittest.TestCase):
    def test_union_and_nulls(self):
        records = [rec("a:1", domain_norm="x.example"), rec("b:1", domain_norm="x.example"),
                   rec("a:2", phone10="4155550101"), rec("b:2", phone10="4155550101"),
                   rec("a:3"), rec("b:3"),  # all-null records never block
                   rec("a:4", name4="quor", postcode5="02110"), rec("b:4", name4="quor", postcode5=None)]
        pairs, per_rule = Blocker.candidates(records)
        self.assertEqual(pairs, {("a:1", "b:1"), ("a:2", "b:2")})
        self.assertEqual(per_rule["domain"], 1)
        self.assertEqual(per_rule["name4_postcode5"], 0)

    def test_true_pairs(self):
        truth = [{"org": "org_a", "Id": "1", "entity_id": "E1"}, {"org": "org_b", "Id": "2", "entity_id": "E1"},
                 {"org": "org_a", "Id": "3", "entity_id": "E1"}, {"org": "org_a", "Id": "4", "entity_id": "E2"}]
        self.assertEqual(len(Blocker.true_pairs(truth)), 3)


class CutoffSelectorTests(unittest.TestCase):
    def setUp(self):
        self.truth = {("a", str(i)) for i in range(10)}

    def test_splink_merge_weight_lowest_meeting_target(self):
        weights = {("a", str(i)): 10.0 + i for i in range(10)}
        weights.update({("n", "1"): 12.0, ("n", "2"): 5.0})
        self.assertEqual(CutoffSelector.splink_merge_weight(weights, self.truth, [0, 6, 12, 13], 0.99), 13.0)
        self.assertIsNone(CutoffSelector.splink_merge_weight({("n", "1"): 50.0}, self.truth, [0, 1], 0.99))

    def test_ai_merge(self):
        ai_p = {("a", str(i)): 0.9 for i in range(10)} | {("n", "1"): 0.8}
        self.assertEqual(CutoffSelector.ai_merge(ai_p, set(), self.truth, [0.5, 0.8, 0.85, 0.95], 0.99), 0.85)
        self.assertIsNone(CutoffSelector.ai_merge({("n", "1"): 0.99}, set(), self.truth, [0.5, 0.99], 0.99))

    def test_ai_nonmatch_highest_within_loss(self):
        ai_p = {("a", "0"): 0.15} | {("n", str(i)): 0.01 for i in range(5)}
        # 10 true pairs, 1% loss allowed -> 0 true pairs may fall at or below the cutoff
        self.assertEqual(CutoffSelector.ai_nonmatch(ai_p, self.truth, 10, [0.02, 0.05, 0.1, 0.2, 0.3], 0.01), 0.1)


class CascadeTests(unittest.TestCase):
    def test_mean_and_missing(self):
        self.assertAlmostEqual(Cascade.mean_answer([0.8, None]).value, 0.8)
        self.assertAlmostEqual(Cascade.mean_answer([0.8, 0.6]).value, 0.7)
        self.assertTrue(Cascade.mean_answer([None, None]).is_missing)
        self.assertTrue(Cascade.mean_answer([]).is_missing)

    def test_outcomes_through_shadow_then_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.jsonl"
            checks = Cascade.checks(0.9, 0.1)
            shadow = DecisionLedger(path, "s", "shadow", checks)
            shadow.finish()
            score = DecisionLedger(path, "r", "score", checks, shadow_run_id="s")
            self.assertEqual(Cascade.ai_outcome(score, "p1", Cascade.mean_answer([0.95, 0.97])), MERGE_AI)
            self.assertEqual(Cascade.ai_outcome(score, "p2", Cascade.mean_answer([0.05])), NOMATCH_AI)
            self.assertEqual(Cascade.ai_outcome(score, "p3", Cascade.mean_answer([0.5, 0.6])), REVIEW_AI)
            self.assertEqual(Cascade.ai_outcome(score, "p4", Cascade.mean_answer([None, None])), REVIEW_MISSING)

    def test_baseline_decisions(self):
        self.assertAlmostEqual(BASELINE_REVIEW_FLOOR_MW, math.log2(0.01 / 0.99))
        d = Cascade.baseline_decisions({("a", "1"): 30.0, ("a", "2"): 0.0, ("a", "3"): -20.0}, 20.0)
        self.assertEqual(d, {("a", "1"): "merge", ("a", "2"): "review", ("a", "3"): "nomatch"})
        self.assertEqual(Cascade.baseline_decisions({("a", "1"): 30.0}, None), {("a", "1"): "review"})


if __name__ == "__main__":
    unittest.main()
