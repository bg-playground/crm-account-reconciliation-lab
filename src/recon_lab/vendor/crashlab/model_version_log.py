"""Per-call model-version logging with mid-run change detection.

Every model call appends one record to an append-only JSONL run log. The
version recorded is the one the provider *returned* (for example the `model`
field of an OpenAI chat completion or an Anthropic message), never the family
name that was requested.

Policy when the returned version changes mid-run, or is missing:

- ``restart``: the violation is logged, then ``ModelVersionChanged`` /
  ``ModelVersionMissing`` is raised so the harness discards and restarts the run.
- ``mark_invalid``: the violation is logged, the run is flagged invalid in the
  log and in the summary, and the run continues.

A missing returned version is never treated as matching the pinned version.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

from .run_log import Clock, JsonlAppendLog, UtcClock

MODEL_CALL = "model_call"
RUN_SUMMARY = "model_version_summary"

STATUS_BASELINE = "baseline"
STATUS_MATCH = "match"
STATUS_CHANGED = "changed"
STATUS_MISSING = "missing"


class VersionChangePolicy(str, Enum):
    RESTART = "restart"
    MARK_INVALID = "mark_invalid"


class ModelVersionError(RuntimeError):
    def __init__(self, message: str, record: "ModelCallRecord") -> None:
        super().__init__(message)
        self.record = record


class ModelVersionChanged(ModelVersionError):
    """The provider returned a different model version than the run's pinned version."""


class ModelVersionMissing(ModelVersionError):
    """The provider response did not carry a usable model version."""


@dataclass(frozen=True)
class ModelCallRecord:
    record_type: str
    timestamp: str
    run_id: str
    call_id: str
    requested_model: str | None
    returned_version: str | None
    pinned_version: str | None
    version_status: str
    run_valid: bool
    policy: str
    provider_call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReturnedVersion:
    """Extract the provider-returned version from a response object or JSON dict."""

    @staticmethod
    def normalize(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def from_response(response: Any) -> str | None:
        if isinstance(response, dict):
            return ReturnedVersion.normalize(response.get("model"))
        return ReturnedVersion.normalize(getattr(response, "model", None))


class ModelVersionLog:
    """Records one entry per model call for a single run and enforces the version pin."""

    def __init__(
        self,
        path: str | Path,
        run_id: str,
        *,
        policy: VersionChangePolicy | str = VersionChangePolicy.RESTART,
        pinned_version: str | None = None,
        clock: Clock | None = None,
        call_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be a non-empty string")
        self.path = Path(path)
        self.run_id = run_id
        self.policy = VersionChangePolicy(policy)
        self._pinned = ReturnedVersion.normalize(pinned_version)
        if pinned_version is not None and self._pinned is None:
            raise ValueError("pinned_version must be a non-empty string when given")
        self._clock = clock
        self._call_id_factory = call_id_factory or (lambda: uuid.uuid4().hex)
        self._lock = threading.Lock()
        self._records: list[ModelCallRecord] = []
        self._violation: ModelVersionError | None = None

    @property
    def pinned_version(self) -> str | None:
        return self._pinned

    @property
    def run_valid(self) -> bool:
        """No violation recorded so far. The summary additionally requires >= 1 call."""
        return self._violation is None

    @property
    def violation(self) -> ModelVersionError | None:
        """The first change/missing violation in this run, if any."""
        return self._violation

    @property
    def restart_required(self) -> bool:
        return self.policy is VersionChangePolicy.RESTART and self._violation is not None

    @property
    def records(self) -> tuple[ModelCallRecord, ...]:
        return tuple(self._records)

    def record(
        self,
        requested_model: str | None,
        returned_version: Any,
        *,
        call_id: str | None = None,
        provider_call_id: str | None = None,
        raise_on_violation: bool = True,
    ) -> ModelCallRecord:
        """Append one call record. Under ``restart`` a violation raises after it is logged.

        Pass ``raise_on_violation=False`` from transport hooks where raising would be
        swallowed or retried by the provider SDK; check ``restart_required`` /
        ``raise_if_restart_required()`` at the next safe point instead.
        """

        version = ReturnedVersion.normalize(returned_version)
        with self._lock:
            if version is None:
                status = STATUS_MISSING
            elif self._pinned is None:
                self._pinned = version
                status = STATUS_BASELINE
            elif version == self._pinned:
                status = STATUS_MATCH
            else:
                status = STATUS_CHANGED

            violated = status in (STATUS_MISSING, STATUS_CHANGED)
            record = ModelCallRecord(
                record_type=MODEL_CALL,
                timestamp=UtcClock.iso(self._clock),
                run_id=self.run_id,
                call_id=call_id or self._call_id_factory(),
                requested_model=requested_model if isinstance(requested_model, str) else None,
                returned_version=version,
                pinned_version=self._pinned,
                version_status=status,
                run_valid=self._violation is None and not violated,
                policy=self.policy.value,
                provider_call_id=provider_call_id,
            )
            JsonlAppendLog.append(self.path, record.as_dict())
            self._records.append(record)
            if violated and self._violation is None:
                self._violation = ModelVersionLog._violation_for(record)

        if violated and raise_on_violation and self.policy is VersionChangePolicy.RESTART:
            raise ModelVersionLog._violation_for(record)
        return record

    def raise_if_restart_required(self) -> None:
        if self.restart_required and self._violation is not None:
            raise self._violation

    def summary(self) -> dict[str, Any]:
        return ModelVersionLog.summarize_records(
            [record.as_dict() for record in self._records],
            run_id=self.run_id,
            policy=self.policy.value,
        )

    def write_summary(self) -> dict[str, Any]:
        summary = self.summary()
        JsonlAppendLog.append(
            self.path,
            {"record_type": RUN_SUMMARY, "timestamp": UtcClock.iso(self._clock), **summary},
        )
        return summary

    @staticmethod
    def _violation_for(record: ModelCallRecord) -> ModelVersionError:
        if record.version_status == STATUS_MISSING:
            return ModelVersionMissing(
                f"run {record.run_id}: call {record.call_id} returned no model version",
                record,
            )
        return ModelVersionChanged(
            f"run {record.run_id}: call {record.call_id} returned {record.returned_version!r}, "
            f"pinned {record.pinned_version!r}",
            record,
        )

    @staticmethod
    def read_records(path: str | Path, run_id: str | None = None) -> list[dict[str, Any]]:
        return [
            record
            for record in JsonlAppendLog.read(path)
            if record.get("record_type") == MODEL_CALL
            and (run_id is None or record.get("run_id") == run_id)
        ]

    @staticmethod
    def summarize_records(
        records: Iterable[dict[str, Any]],
        *,
        run_id: str,
        policy: str,
    ) -> dict[str, Any]:
        """Recompute the run summary from logged records (audit path; no in-memory state)."""

        calls = [r for r in records if r.get("run_id") == run_id and r.get("record_type") == MODEL_CALL]
        changed = [r["call_id"] for r in calls if r.get("version_status") == STATUS_CHANGED]
        missing = [r["call_id"] for r in calls if r.get("version_status") == STATUS_MISSING]
        versions_seen = sorted({r["returned_version"] for r in calls if r.get("returned_version")})
        pinned = next((r["pinned_version"] for r in calls if r.get("pinned_version")), None)
        invalid_reasons: list[str] = []
        if changed:
            invalid_reasons.append("model_version_changed")
        if missing:
            invalid_reasons.append("model_version_missing")
        if not calls:
            invalid_reasons.append("no_model_calls_recorded")
        return {
            "run_id": run_id,
            "policy": policy,
            "calls": len(calls),
            "pinned_version": pinned,
            "versions_seen": versions_seen,
            "changed_call_ids": changed,
            "missing_call_ids": missing,
            "run_valid": not invalid_reasons,
            "invalid_reasons": invalid_reasons,
        }


class ModelVersionHttpHook:
    """httpx response hook that logs the returned `model` of OpenAI-compatible calls.

    Works for any SDK that accepts an ``httpx.AsyncClient`` (e.g. the OpenAI
    SDK). Only successful (2xx)
    responses on the configured paths are recorded, so SDK retries of failed
    attempts are not counted as calls. The hook never raises into the SDK
    (OpenAI's client would retry and wrap the error); under ``restart`` the
    caller must poll ``log.restart_required`` or call
    ``log.raise_if_restart_required()``.
    """

    DEFAULT_PATH_SUFFIXES: tuple[str, ...] = ("/chat/completions", "/responses", "/messages")

    def __init__(self, log: ModelVersionLog, path_suffixes: Sequence[str] | None = None) -> None:
        self.log = log
        self.path_suffixes = tuple(path_suffixes or ModelVersionHttpHook.DEFAULT_PATH_SUFFIXES)

    async def __call__(self, response: httpx.Response) -> None:
        request = response.request
        if not request.url.path.endswith(self.path_suffixes):
            return
        if not response.is_success:
            return
        await response.aread()
        payload = ModelVersionHttpHook._json_object(response.content)
        requested = ModelVersionHttpHook._json_object(request.content).get("model")
        provider_call_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        self.log.record(
            requested if isinstance(requested, str) else None,
            ReturnedVersion.from_response(payload),
            provider_call_id=provider_call_id,
            raise_on_violation=False,
        )

    @staticmethod
    def _json_object(content: bytes) -> dict[str, Any]:
        try:
            value = json.loads(content or b"null")
        except (ValueError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def async_client(
        log: ModelVersionLog,
        *,
        path_suffixes: Sequence[str] | None = None,
        **client_kwargs: Any,
    ) -> httpx.AsyncClient:
        hooks = client_kwargs.pop("event_hooks", {}) or {}
        response_hooks = list(hooks.get("response", []))
        response_hooks.append(ModelVersionHttpHook(log, path_suffixes))
        return httpx.AsyncClient(
            event_hooks={**hooks, "response": response_hooks},
            **client_kwargs,
        )
