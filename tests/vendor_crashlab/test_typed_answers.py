from __future__ import annotations

import unittest

from recon_lab.vendor.crashlab.typed_answers import (
    ChoiceQuestion,
    MissingAnswer,
    MissingReason,
    ParsedAnswer,
    ScoreQuestion,
    TypedAnswerParser,
    YesNoQuestion,
)

YES_NO = YesNoQuestion("refund_needed")
CHOICE = ChoiceQuestion("department", ("billing", "technical", "sales"))
SCORE = ScoreQuestion("frustration", 1, 5)


class WellFormedAnswerTests(unittest.TestCase):
    def test_yes_no_with_probability(self) -> None:
        answer = TypedAnswerParser.parse(YES_NO, '{"type": "yes_no", "probability": 0.2}')
        self.assertIsInstance(answer, ParsedAnswer)
        self.assertEqual(answer.value, 0.2)
        self.assertAlmostEqual(answer.confidence, 0.8)

    def test_choice_value_comes_from_supplied_list(self) -> None:
        answer = TypedAnswerParser.parse(CHOICE, {"type": "choice", "id": "billing", "confidence": 0.7})
        self.assertIsInstance(answer, ParsedAnswer)
        self.assertEqual(answer.value, "billing")
        self.assertIs(answer.value, CHOICE.allowed_ids[0])
        self.assertEqual(answer.confidence, 0.7)

    def test_choice_confidence_is_optional(self) -> None:
        answer = TypedAnswerParser.parse(CHOICE, {"type": "choice", "id": "sales"})
        self.assertIsNone(answer.confidence)

    def test_score_on_fixed_scale(self) -> None:
        answer = TypedAnswerParser.parse(SCORE, b'{"type": "score", "score": 4.0, "confidence": 0.6}')
        self.assertIsInstance(answer, ParsedAnswer)
        self.assertEqual(answer.value, 4)


class MalformedAnswerTests(unittest.TestCase):
    CASES = (
        (YES_NO, "{not json", MissingReason.BAD_JSON),
        (YES_NO, None, MissingReason.BAD_JSON),
        (YES_NO, "[0.4]", MissingReason.NOT_AN_OBJECT),
        (YES_NO, '"yes"', MissingReason.NOT_AN_OBJECT),
        (YES_NO, {"type": "choice", "probability": 0.4}, MissingReason.WRONG_TYPE),
        (YES_NO, {"probability": 0.4}, MissingReason.WRONG_TYPE),
        (YES_NO, {"type": "yes_no"}, MissingReason.MISSING_FIELD),
        (YES_NO, {"type": "yes_no", "probability": "0.4"}, MissingReason.NOT_A_NUMBER),
        (YES_NO, {"type": "yes_no", "probability": True}, MissingReason.NOT_A_NUMBER),
        (YES_NO, '{"type": "yes_no", "probability": NaN}', MissingReason.NON_FINITE),
        (YES_NO, {"type": "yes_no", "probability": 10**400}, MissingReason.NON_FINITE),
        (YES_NO, {"type": "yes_no", "probability": 1.01}, MissingReason.PROBABILITY_OUT_OF_RANGE),
        (YES_NO, {"type": "yes_no", "probability": -0.1}, MissingReason.PROBABILITY_OUT_OF_RANGE),
        (CHOICE, {"type": "choice", "id": "legal"}, MissingReason.ID_NOT_ALLOWED),
        (CHOICE, {"type": "choice", "id": "Billing"}, MissingReason.ID_NOT_ALLOWED),
        (CHOICE, {"type": "choice", "id": 0}, MissingReason.WRONG_TYPE),
        (CHOICE, {"type": "choice"}, MissingReason.MISSING_FIELD),
        (CHOICE, {"type": "choice", "id": "billing", "confidence": 2}, MissingReason.CONFIDENCE_OUT_OF_RANGE),
        (CHOICE, {"type": "choice", "id": "billing", "confidence": "high"}, MissingReason.NOT_A_NUMBER),
        (SCORE, {"type": "score", "score": 6}, MissingReason.SCORE_OUT_OF_RANGE),
        (SCORE, {"type": "score", "score": 0}, MissingReason.SCORE_OUT_OF_RANGE),
        (SCORE, {"type": "score", "score": 3.5}, MissingReason.SCORE_NOT_ON_SCALE),
        (SCORE, {"type": "score", "score": "4"}, MissingReason.NOT_A_NUMBER),
    )

    def test_every_malformed_case_is_missing_with_reason(self) -> None:
        for question, raw, reason in self.CASES:
            with self.subTest(question=question.name, raw=raw):
                answer = TypedAnswerParser.parse(question, raw)
                self.assertIsInstance(answer, MissingAnswer)
                self.assertTrue(answer.is_missing)
                self.assertEqual(answer.reason, reason)
                self.assertEqual(answer.check, f"typed_answer.{reason.value}")

    def test_fractional_scores_allowed_only_when_configured(self) -> None:
        question = ScoreQuestion("risk", 0, 10, integer_only=False)
        answer = TypedAnswerParser.parse(question, {"type": "score", "score": 3.5})
        self.assertIsInstance(answer, ParsedAnswer)
        self.assertEqual(answer.value, 3.5)

    def test_question_definitions_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            ChoiceQuestion("empty", ())
        with self.assertRaises(ValueError):
            ChoiceQuestion("dupes", ("a", "a"))
        with self.assertRaises(ValueError):
            ScoreQuestion("flat", 3, 3)


if __name__ == "__main__":
    unittest.main()
