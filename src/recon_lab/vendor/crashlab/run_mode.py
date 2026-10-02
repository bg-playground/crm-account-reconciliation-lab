"""Shadow-then-score switch with frozen thresholds and a rejection log.

- ``shadow`` mode records every decision (accept, reject, missing) with the
  check that fired, but never runs the action and never counts as scored.
- ``score`` mode runs the action only on ``accept`` and counts as scored. It
  refuses to start unless the same log already holds a shadow run that used
  the identical frozen thresholds (same fingerprint).

Code decides: a ``ThresholdCheck`` compares a typed answer against a frozen
threshold. A ``MissingAnswer`` always yields ``missing`` (never ``accept``).
"""

from __future__ import annotations

import hashlib
import json
import math
import operator
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from .run_log import Clock, JsonlAppendLog, UtcClock
from .typed_answers import MissingAnswer, ParsedAnswer, TypedAnswer

T = TypeVar("T")

RUN_START = "decision_run_start"
DECISION = "decision"
RUN_SUMMARY = "decision_run_summary"

ACCEPT = "accept"
REJECT = "reject"
MISSING = "missing"
OUTCOMES = (ACCEPT, REJECT, MISSING)

_OPERATORS: Mapping[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
}


class RunMode(str, Enum):
    SHADOW = "shadow"
    SCORE = "score"


class ShadowRunRequired(RuntimeError):
    """Score mode was requested without a matching prior shadow run."""


class ThresholdDrift(RuntimeError):
    """Score mode thresholds differ from the shadow run's frozen thresholds."""


@dataclass(frozen=True)
class ThresholdCheck:
    """Accept when ``answer.<field> <op> threshold``. Fields: ``value`` or ``confidence``."""

    name: str
    field: str
    op: str
    threshold: float

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("a threshold check needs a name")
        if self.field not in ("value", "confidence"):
            raise ValueError("field must be 'value' or 'confidence'")
        if self.op not in _OPERATORS:
            raise ValueError(f"op must be one of {sorted(_OPERATORS)}")
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, (int, float)):
            raise TypeError("threshold must be a number")
        if not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite")

    def evaluate(self, answer: TypedAnswer) -> tuple[str, str, str]:
        """Return (outcome, check that fired, detail)."""

        if isinstance(answer, MissingAnswer):
            return MISSING, answer.check, answer.detail
        observed = getattr(answer, self.field)
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            return MISSING, f"{self.name}.no_numeric_{self.field}", f"{self.field} is not numeric"
        if _OPERATORS[self.op](observed, self.threshold):
            return ACCEPT, self.name, f"{self.field}={observed} {self.op} {self.threshold}"
        return REJECT, self.name, f"{self.field}={observed} not {self.op} {self.threshold}"


class FrozenThresholds:
    @staticmethod
    def canonical(checks: Iterable[ThresholdCheck]) -> list[dict[str, Any]]:
        return sorted((asdict(check) for check in checks), key=lambda item: item["name"])

    @staticmethod
    def fingerprint(checks: Iterable[ThresholdCheck]) -> str:
        payload = json.dumps(FrozenThresholds.canonical(checks), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DecisionRecord:
    record_type: str
    timestamp: str
    run_id: str
    mode: str
    step: str
    outcome: str
    check: str | None
    detail: str
    acted: bool
    scored: bool
    action_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionLedger:
    """Append-only decision log for one run in one mode."""

    def __init__(
        self,
        path: str | Path,
        run_id: str,
        mode: RunMode | str,
        checks: Iterable[ThresholdCheck],
        *,
        shadow_run_id: str | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        self.path = Path(path)
        self.run_id = run_id
        self.mode = RunMode(mode)
        self.checks = {check.name: check for check in checks}
        if not self.checks:
            raise ValueError("at least one frozen threshold check is required")
        self.thresholds_fingerprint = FrozenThresholds.fingerprint(self.checks.values())
        self._clock = clock
        self._records: list[DecisionRecord] = []

        if self.mode is RunMode.SCORE:
            DecisionLedger.require_shadow_run(self.path, shadow_run_id, self.thresholds_fingerprint)
        elif shadow_run_id is not None:
            raise ValueError("shadow_run_id only applies to score mode")

        JsonlAppendLog.append(
            self.path,
            {
                "record_type": RUN_START,
                "timestamp": UtcClock.iso(self._clock),
                "run_id": run_id,
                "mode": self.mode.value,
                "shadow_run_id": shadow_run_id,
                "thresholds": FrozenThresholds.canonical(self.checks.values()),
                "thresholds_fingerprint": self.thresholds_fingerprint,
            },
        )

    @staticmethod
    def require_shadow_run(path: Path, shadow_run_id: str | None, fingerprint: str) -> None:
        if not shadow_run_id:
            raise ShadowRunRequired("score mode needs the run id of a completed shadow run")
        records = JsonlAppendLog.read(path)
        starts = [
            r
            for r in records
            if r.get("record_type") == RUN_START
            and r.get("run_id") == shadow_run_id
            and r.get("mode") == RunMode.SHADOW.value
        ]
        if not starts:
            raise ShadowRunRequired(f"no shadow run {shadow_run_id!r} in {path.name}")
        if not any(
            r.get("record_type") == RUN_SUMMARY and r.get("run_id") == shadow_run_id for r in records
        ):
            raise ShadowRunRequired(f"shadow run {shadow_run_id!r} has no summary; finish it first")
        if starts[-1].get("thresholds_fingerprint") != fingerprint:
            raise ThresholdDrift("score thresholds differ from the shadow run's frozen thresholds")

    def decide(
        self,
        step: str,
        check_name: str,
        answer: TypedAnswer,
        action: Callable[[], T] | None = None,
    ) -> DecisionRecord:
        """Evaluate one typed answer against one frozen check and log the decision."""

        if check_name not in self.checks:
            raise KeyError(f"unknown frozen check {check_name!r}")
        if not isinstance(answer, (ParsedAnswer, MissingAnswer)):
            raise TypeError("answer must come from TypedAnswerParser")
        outcome, fired, detail = self.checks[check_name].evaluate(answer)
        scored = self.mode is RunMode.SCORE
        should_act = scored and outcome == ACCEPT and action is not None
        action_error: str | None = None
        failure: BaseException | None = None
        if should_act:
            try:
                action()
            except Exception as exc:  # recorded, then re-raised
                action_error = type(exc).__name__
                failure = exc
        record = DecisionRecord(
            record_type=DECISION,
            timestamp=UtcClock.iso(self._clock),
            run_id=self.run_id,
            mode=self.mode.value,
            step=step,
            outcome=outcome,
            check=fired,
            detail=detail,
            acted=should_act and failure is None,
            scored=scored,
            action_error=action_error,
        )
        JsonlAppendLog.append(self.path, record.as_dict())
        self._records.append(record)
        if failure is not None:
            raise failure
        return record

    def summary(self) -> dict[str, Any]:
        return DecisionLedger.summarize_records(
            [record.as_dict() for record in self._records], run_id=self.run_id, mode=self.mode.value
        )

    def finish(self) -> dict[str, Any]:
        summary = self.summary()
        JsonlAppendLog.append(
            self.path,
            {
                "record_type": RUN_SUMMARY,
                "timestamp": UtcClock.iso(self._clock),
                "thresholds_fingerprint": self.thresholds_fingerprint,
                **summary,
            },
        )
        return summary

    @staticmethod
    def summarize_records(records: Iterable[dict[str, Any]], *, run_id: str, mode: str) -> dict[str, Any]:
        decisions = [r for r in records if r.get("record_type") == DECISION and r.get("run_id") == run_id]
        outcomes = Counter(r["outcome"] for r in decisions)
        rejections = Counter(r["check"] for r in decisions if r["outcome"] in (REJECT, MISSING))
        return {
            "run_id": run_id,
            "mode": mode,
            "decisions": len(decisions),
            "accepted": outcomes.get(ACCEPT, 0),
            "rejected": outcomes.get(REJECT, 0),
            "missing": outcomes.get(MISSING, 0),
            "rejections_by_check": dict(sorted(rejections.items())),
            "actions_taken": sum(1 for r in decisions if r.get("acted")),
            "scored_decisions": sum(1 for r in decisions if r.get("scored")),
        }
