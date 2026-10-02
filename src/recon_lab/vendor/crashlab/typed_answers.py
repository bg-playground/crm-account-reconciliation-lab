"""Typed-answer parsing: malformed answers become explicit MISSING results.

The model may answer only three narrow question types:

- ``yes_no``: ``{"type": "yes_no", "probability": 0.83}`` (probability of "yes")
- ``choice``: ``{"type": "choice", "id": "acct-3", "confidence": 0.7}`` where the
  id must be in the allowed list supplied by code; ``confidence`` is optional
- ``score``:  ``{"type": "score", "score": 4, "confidence": 0.6}`` on a fixed
  scale ``[minimum, maximum]``; ``confidence`` is optional

Anything else (bad JSON, wrong type, an id outside the list, a probability or
confidence outside [0, 1], a non-finite number, an out-of-range score) becomes a
``MissingAnswer`` that carries the reason. A ``MissingAnswer`` is never a pass.
Extra keys are ignored. For a choice, the value returned is the id from the
supplied list, never text written by the model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union

YES_NO = "yes_no"
CHOICE = "choice"
SCORE = "score"


class MissingReason(str, Enum):
    BAD_JSON = "bad_json"
    NOT_AN_OBJECT = "not_an_object"
    WRONG_TYPE = "wrong_type"
    MISSING_FIELD = "missing_field"
    NOT_A_NUMBER = "not_a_number"
    NON_FINITE = "non_finite"
    PROBABILITY_OUT_OF_RANGE = "probability_out_of_range"
    CONFIDENCE_OUT_OF_RANGE = "confidence_out_of_range"
    ID_NOT_ALLOWED = "id_not_allowed"
    SCORE_OUT_OF_RANGE = "score_out_of_range"
    SCORE_NOT_ON_SCALE = "score_not_on_scale"


@dataclass(frozen=True)
class YesNoQuestion:
    name: str
    kind: str = YES_NO


@dataclass(frozen=True)
class ChoiceQuestion:
    name: str
    allowed_ids: tuple[str, ...]
    kind: str = CHOICE

    def __post_init__(self) -> None:
        if not self.allowed_ids:
            raise ValueError("a choice question needs at least one allowed id")
        if len(set(self.allowed_ids)) != len(self.allowed_ids):
            raise ValueError("allowed ids must be unique")
        if not all(isinstance(item, str) and item for item in self.allowed_ids):
            raise ValueError("allowed ids must be non-empty strings")


@dataclass(frozen=True)
class ScoreQuestion:
    name: str
    minimum: int
    maximum: int
    integer_only: bool = True
    kind: str = SCORE

    def __post_init__(self) -> None:
        if self.maximum <= self.minimum:
            raise ValueError("score maximum must be greater than minimum")


Question = Union[YesNoQuestion, ChoiceQuestion, ScoreQuestion]


@dataclass(frozen=True)
class ParsedAnswer:
    question: str
    kind: str
    value: float | int | str
    confidence: float | None
    is_missing: bool = False


@dataclass(frozen=True)
class MissingAnswer:
    question: str
    kind: str
    reason: MissingReason
    detail: str
    is_missing: bool = True

    @property
    def check(self) -> str:
        """Name of the check that fired, for rejection logs."""
        return f"typed_answer.{self.reason.value}"


TypedAnswer = Union[ParsedAnswer, MissingAnswer]


class TypedAnswerParser:
    @staticmethod
    def parse(question: Question, raw: Any) -> TypedAnswer:
        """Parse raw model output (JSON text, bytes, or a decoded dict)."""

        if raw is None:
            return TypedAnswerParser._missing(question, MissingReason.BAD_JSON, "no answer returned")
        if isinstance(raw, (str, bytes, bytearray)):
            try:
                raw = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                return TypedAnswerParser._missing(question, MissingReason.BAD_JSON, type(exc).__name__)
        if not isinstance(raw, dict):
            return TypedAnswerParser._missing(
                question, MissingReason.NOT_AN_OBJECT, f"got {type(raw).__name__}"
            )
        if raw.get("type") != question.kind:
            return TypedAnswerParser._missing(
                question, MissingReason.WRONG_TYPE, f"expected type {question.kind!r}"
            )
        if isinstance(question, YesNoQuestion):
            return TypedAnswerParser._yes_no(question, raw)
        if isinstance(question, ChoiceQuestion):
            return TypedAnswerParser._choice(question, raw)
        return TypedAnswerParser._score(question, raw)

    @staticmethod
    def _yes_no(question: YesNoQuestion, raw: dict[str, Any]) -> TypedAnswer:
        number = TypedAnswerParser._number(question, raw, "probability")
        if isinstance(number, MissingAnswer):
            return number
        if not 0.0 <= number <= 1.0:
            return TypedAnswerParser._missing(
                question, MissingReason.PROBABILITY_OUT_OF_RANGE, "probability must be within [0, 1]"
            )
        return ParsedAnswer(question.name, question.kind, float(number), max(number, 1.0 - number))

    @staticmethod
    def _choice(question: ChoiceQuestion, raw: dict[str, Any]) -> TypedAnswer:
        if "id" not in raw:
            return TypedAnswerParser._missing(question, MissingReason.MISSING_FIELD, "id")
        chosen = raw["id"]
        if not isinstance(chosen, str):
            return TypedAnswerParser._missing(question, MissingReason.WRONG_TYPE, "id must be a string")
        if chosen not in question.allowed_ids:
            return TypedAnswerParser._missing(
                question, MissingReason.ID_NOT_ALLOWED, "id is not in the supplied list"
            )
        confidence = TypedAnswerParser._confidence(question, raw)
        if isinstance(confidence, MissingAnswer):
            return confidence
        supplied = question.allowed_ids[question.allowed_ids.index(chosen)]
        return ParsedAnswer(question.name, question.kind, supplied, confidence)

    @staticmethod
    def _score(question: ScoreQuestion, raw: dict[str, Any]) -> TypedAnswer:
        number = TypedAnswerParser._number(question, raw, "score")
        if isinstance(number, MissingAnswer):
            return number
        if not question.minimum <= number <= question.maximum:
            return TypedAnswerParser._missing(
                question,
                MissingReason.SCORE_OUT_OF_RANGE,
                f"score must be within [{question.minimum}, {question.maximum}]",
            )
        if question.integer_only and float(number) != int(number):
            return TypedAnswerParser._missing(
                question, MissingReason.SCORE_NOT_ON_SCALE, "score must be a whole scale level"
            )
        confidence = TypedAnswerParser._confidence(question, raw)
        if isinstance(confidence, MissingAnswer):
            return confidence
        value: float | int = int(number) if question.integer_only else float(number)
        return ParsedAnswer(question.name, question.kind, value, confidence)

    @staticmethod
    def _confidence(question: Question, raw: dict[str, Any]) -> float | None | MissingAnswer:
        if "confidence" not in raw or raw["confidence"] is None:
            return None
        number = TypedAnswerParser._number(question, raw, "confidence")
        if isinstance(number, MissingAnswer):
            return number
        if not 0.0 <= number <= 1.0:
            return TypedAnswerParser._missing(
                question, MissingReason.CONFIDENCE_OUT_OF_RANGE, "confidence must be within [0, 1]"
            )
        return float(number)

    @staticmethod
    def _number(question: Question, raw: dict[str, Any], field: str) -> float | int | MissingAnswer:
        if field not in raw:
            return TypedAnswerParser._missing(question, MissingReason.MISSING_FIELD, field)
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return TypedAnswerParser._missing(question, MissingReason.NOT_A_NUMBER, f"{field} must be a number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            return TypedAnswerParser._missing(question, MissingReason.NON_FINITE, f"{field} must be finite")
        return value

    @staticmethod
    def _missing(question: Question, reason: MissingReason, detail: str) -> MissingAnswer:
        return MissingAnswer(question.name, question.kind, reason, detail)
