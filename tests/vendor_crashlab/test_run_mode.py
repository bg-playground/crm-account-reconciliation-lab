from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from recon_lab.vendor.crashlab.run_mode import (
    DecisionLedger,
    FrozenThresholds,
    RunMode,
    ShadowRunRequired,
    ThresholdCheck,
    ThresholdDrift,
)
from recon_lab.vendor.crashlab.typed_answers import ChoiceQuestion, TypedAnswerParser, YesNoQuestion

FIXED = datetime(2026, 10, 1, 13, 40, tzinfo=timezone.utc)
CHECKS = (
    ThresholdCheck("refund_probability_floor", "value", ">=", 0.8),
    ThresholdCheck("department_confidence_floor", "confidence", ">=", 0.6),
)
REFUND = YesNoQuestion("refund_needed")
DEPARTMENT = ChoiceQuestion("department", ("billing", "technical"))


def yes(probability: float):
    return TypedAnswerParser.parse(REFUND, {"type": "yes_no", "probability": probability})


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "decisions.jsonl"
        self.actions: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def ledger(self, run_id: str, mode: RunMode, checks=CHECKS, **kwargs) -> DecisionLedger:
        return DecisionLedger(self.path, run_id, mode, checks, clock=lambda: FIXED, **kwargs)

    def act(self, name: str):
        return lambda: self.actions.append(name)

    def finished_shadow(self, checks=CHECKS) -> DecisionLedger:
        shadow = self.ledger("shadow-1", RunMode.SHADOW, checks)
        shadow.decide("s1", "refund_probability_floor", yes(0.9), self.act("refund"))
        shadow.finish()
        return shadow


class ShadowModeTests(LedgerCase):
    def test_shadow_logs_decisions_and_rejections_but_takes_no_action(self) -> None:
        ledger = self.ledger("shadow-1", RunMode.SHADOW)
        accepted = ledger.decide("s1", "refund_probability_floor", yes(0.9), self.act("refund"))
        rejected = ledger.decide("s2", "refund_probability_floor", yes(0.5), self.act("refund"))
        missing = ledger.decide("s3", "refund_probability_floor", TypedAnswerParser.parse(REFUND, "oops"), self.act("refund"))
        self.assertEqual(self.actions, [])
        self.assertEqual((accepted.outcome, accepted.acted, accepted.scored), ("accept", False, False))
        self.assertEqual((rejected.outcome, rejected.check), ("reject", "refund_probability_floor"))
        self.assertEqual((missing.outcome, missing.check), ("missing", "typed_answer.bad_json"))
        summary = ledger.finish()
        self.assertEqual(summary["actions_taken"], 0)
        self.assertEqual(summary["scored_decisions"], 0)
        self.assertEqual(
            summary["rejections_by_check"],
            {"refund_probability_floor": 1, "typed_answer.bad_json": 1},
        )


class ScoreModeTests(LedgerCase):
    def test_score_requires_a_finished_shadow_run(self) -> None:
        with self.assertRaises(ShadowRunRequired):
            self.ledger("score-1", RunMode.SCORE)
        with self.assertRaises(ShadowRunRequired):
            self.ledger("score-1", RunMode.SCORE, shadow_run_id="nope")
        unfinished = self.ledger("shadow-x", RunMode.SHADOW)
        unfinished.decide("s1", "refund_probability_floor", yes(0.9))
        with self.assertRaises(ShadowRunRequired):
            self.ledger("score-1", RunMode.SCORE, shadow_run_id="shadow-x")

    def test_score_refuses_thresholds_that_drifted_from_shadow(self) -> None:
        self.finished_shadow()
        drifted = (ThresholdCheck("refund_probability_floor", "value", ">=", 0.7), CHECKS[1])
        with self.assertRaises(ThresholdDrift):
            self.ledger("score-1", RunMode.SCORE, drifted, shadow_run_id="shadow-1")

    def test_score_acts_only_on_accept_and_counts(self) -> None:
        self.finished_shadow()
        ledger = self.ledger("score-1", RunMode.SCORE, shadow_run_id="shadow-1")
        ledger.decide("s1", "refund_probability_floor", yes(0.9), self.act("refund-1"))
        ledger.decide("s2", "refund_probability_floor", yes(0.3), self.act("refund-2"))
        ledger.decide(
            "s3",
            "department_confidence_floor",
            TypedAnswerParser.parse(DEPARTMENT, {"type": "choice", "id": "legal", "confidence": 0.99}),
            self.act("route"),
        )
        ledger.decide(
            "s4",
            "department_confidence_floor",
            TypedAnswerParser.parse(DEPARTMENT, {"type": "choice", "id": "billing"}),
            self.act("route-no-confidence"),
        )
        self.assertEqual(self.actions, ["refund-1"])
        summary = ledger.finish()
        self.assertEqual(summary["scored_decisions"], 4)
        self.assertEqual(summary["actions_taken"], 1)
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(summary["missing"], 2, "a missing answer is never a pass")
        self.assertEqual(
            summary["rejections_by_check"],
            {
                "department_confidence_floor.no_numeric_confidence": 1,
                "refund_probability_floor": 1,
                "typed_answer.id_not_allowed": 1,
            },
        )

    def test_action_failure_is_logged_then_raised(self) -> None:
        self.finished_shadow()
        ledger = self.ledger("score-1", RunMode.SCORE, shadow_run_id="shadow-1")

        def boom() -> None:
            raise RuntimeError("downstream refused")

        with self.assertRaises(RuntimeError):
            ledger.decide("s1", "refund_probability_floor", yes(0.95), boom)
        last = json.loads(self.path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((last["outcome"], last["acted"], last["action_error"]), ("accept", False, "RuntimeError"))


class FrozenThresholdTests(LedgerCase):
    def test_fingerprint_is_order_independent_and_logged(self) -> None:
        self.assertEqual(FrozenThresholds.fingerprint(CHECKS), FrozenThresholds.fingerprint(CHECKS[::-1]))
        ledger = self.ledger("shadow-1", RunMode.SHADOW)
        start = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(start["record_type"], "decision_run_start")
        self.assertEqual(start["thresholds_fingerprint"], ledger.thresholds_fingerprint)

    def test_unknown_check_and_bad_definitions_are_rejected(self) -> None:
        ledger = self.ledger("shadow-1", RunMode.SHADOW)
        with self.assertRaises(KeyError):
            ledger.decide("s1", "not_frozen", yes(0.9))
        with self.assertRaises(ValueError):
            ThresholdCheck("x", "value", "==", 0.5)
        with self.assertRaises(ValueError):
            ThresholdCheck("x", "probability", ">=", 0.5)
        with self.assertRaises(ValueError):
            self.ledger("shadow-2", RunMode.SHADOW, shadow_run_id="shadow-1")


if __name__ == "__main__":
    unittest.main()
