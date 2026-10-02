"""Typed AI judge for candidate Account pairs, with spend tracking.

The API key is read from the OPENAI_API_KEY environment variable by the OpenAI
SDK and is never logged, printed or written to disk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vendor.crashlab.model_version_log import ModelVersionHttpHook, ModelVersionLog
from .vendor.crashlab.run_log import JsonlAppendLog, UtcClock
from .vendor.crashlab.typed_answers import MissingAnswer, TypedAnswerParser, YesNoQuestion

PROMPT_FIELDS = ["Name", "Website", "Phone", "BillingStreet", "BillingCity", "BillingState",
                 "BillingPostalCode", "BillingCountry", "Industry", "NumberOfEmployees"]
QUESTION = YesNoQuestion("same_organization")
DEFAULT_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "account_match.v1.md"


class PromptBuilder:
    @staticmethod
    def load(path: Path = DEFAULT_PROMPT) -> tuple[str, str, str]:
        """Return (system, user_template, sha256 of the prompt file bytes)."""

        raw = path.read_bytes()
        text = raw.decode("utf-8")
        system, user = text.split("## User", 1)
        system = system.replace("## System", "", 1).strip()
        return system, user.strip(), hashlib.sha256(raw).hexdigest()

    @staticmethod
    def render_record(row: dict[str, str], name_override: str | None = None) -> str:
        lines = []
        for field in PROMPT_FIELDS:
            value = name_override if (field == "Name" and name_override is not None) else row.get(field, "")
            lines.append(f"{field}: {value if value else '(blank)'}")
        return "\n".join(lines)

    @staticmethod
    def messages(system: str, template: str, left: dict[str, str], right: dict[str, str], *,
                 order_seed: str, names: tuple[str, str] | None = None) -> list[dict[str, str]]:
        """Records go in a deterministic pseudo-random order; Ids and ground truth never enter the prompt."""

        first, second = (left, right)
        n1, n2 = names if names else (None, None)
        if random.Random(order_seed).random() < 0.5:
            first, second, n1, n2 = second, first, n2, n1
        user = template.replace("{record_1}", PromptBuilder.render_record(first, n1)).replace(
            "{record_2}", PromptBuilder.render_record(second, n2))
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class SpendLimitReached(RuntimeError):
    pass


class SpendLedger:
    """Estimated spend from token usage. Cached input is charged at the full input price."""

    def __init__(self, path: Path, phase: str, *, input_per_m: float, output_per_m: float,
                 phase_cap_usd: float, stop_at_total_usd: float, hard_ceiling_usd: float) -> None:
        self.path = Path(path)
        self.phase = phase
        self.input_per_m = input_per_m
        self.output_per_m = output_per_m
        self.phase_cap = phase_cap_usd
        self.stop_at_total = min(stop_at_total_usd, hard_ceiling_usd)
        self.prior_total = SpendLedger.total(self.path)
        self.prior_total_exposure = SpendLedger.exposure(self.path)
        self.prior_phase = SpendLedger.total(self.path, phase)
        self.prior_phase_exposure = SpendLedger.exposure(self.path, phase)
        self._lock = threading.RLock()
        self._reservations: dict[object, float] = {}
        self.phase_spend = 0.0
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    @staticmethod
    def cost(input_tokens: int, output_tokens: int, input_per_m: float, output_per_m: float) -> float:
        return input_tokens * input_per_m / 1_000_000 + output_tokens * output_per_m / 1_000_000

    @staticmethod
    def total(path: Path, phase: str | None = None) -> float:
        if not Path(path).exists():
            return 0.0
        return sum(r["est_usd"] for r in JsonlAppendLog.read(path)
                   if r.get("record_type") == "spend" and (phase is None or r.get("phase") == phase))

    @property
    def cumulative(self) -> float:
        return self.prior_total + self.phase_spend

    @staticmethod
    def exposure(path: Path, phase: str | None = None) -> float:
        """Actual estimates plus unresolved reservations from prior runs."""
        if not Path(path).exists():
            return 0.0
        return sum(r["est_usd"] + r.get("unresolved_reserved_usd", 0.0)
                   for r in JsonlAppendLog.read(path)
                   if r.get("record_type") == "spend" and (phase is None or r.get("phase") == phase))

    @property
    def reserved_usd(self) -> float:
        with self._lock:
            return sum(self._reservations.values())

    def allow(self, worst_case_usd: float) -> bool:
        """Check estimated exposure, including all outstanding calls."""
        if not math.isfinite(worst_case_usd) or worst_case_usd < 0:
            raise ValueError("Reservation must be finite and nonnegative")
        with self._lock:
            exposure = self.phase_spend + self.reserved_usd + worst_case_usd
            return (self.prior_phase_exposure + exposure <= self.phase_cap
                    and self.prior_total_exposure + exposure < self.stop_at_total)

    def reserve(self, worst_case_usd: float) -> object | None:
        """Atomically admit one attempt; no await separates check and reservation."""
        with self._lock:
            if not self.allow(worst_case_usd):
                return None
            token = object()
            self._reservations[token] = worst_case_usd
            return token

    def settle(self, reservation: object, input_tokens: int, output_tokens: int) -> float:
        """Replace a reservation with reported usage exactly once."""
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
            raise ValueError("Usage must contain nonnegative integer token counts")
        with self._lock:
            if reservation not in self._reservations:
                raise ValueError("Unknown or already settled reservation")
            usd = self.add(input_tokens, output_tokens)
            del self._reservations[reservation]
            return usd

    def add(self, input_tokens: int, output_tokens: int) -> float:
        with self._lock:
            usd = SpendLedger.cost(input_tokens, output_tokens, self.input_per_m, self.output_per_m)
            self.phase_spend += usd
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            return usd

    def flush(self, run_id: str, note: str = "") -> dict[str, Any]:
        record = {"record_type": "spend", "timestamp": UtcClock.iso(), "run_id": run_id, "phase": self.phase,
                  "calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                  "est_usd": round(self.phase_spend, 6), "cumulative_est_usd": round(self.cumulative, 6),
                  "price_input_per_m": self.input_per_m, "price_output_per_m": self.output_per_m, "note": note}
        if self.reserved_usd:
            record["unresolved_reserved_usd"] = self.reserved_usd
            record["unresolved_calls"] = len(self._reservations)
        JsonlAppendLog.append(self.path, record)
        return record


@dataclass(frozen=True)
class JudgeRequest:
    pair_id: str
    arm: str
    repeat: int
    messages: list[dict[str, str]]


class JudgeClient:
    """Runs judge requests concurrently; every failure becomes a typed MissingAnswer."""

    def __init__(self, *, model: str, version_log: ModelVersionLog, spend: SpendLedger, calls_path: Path,
                 max_completion_tokens: int, reasoning_effort: str | None, concurrency: int = 8,
                 worst_case_input_tokens: int = 1200, transport: Any = None, base_url: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self.version_log = version_log
        self.spend = spend
        self.calls_path = Path(calls_path)
        self.max_completion_tokens = max_completion_tokens
        self.reasoning_effort = reasoning_effort
        self.semaphore = asyncio.Semaphore(concurrency)
        self.worst_case = SpendLedger.cost(worst_case_input_tokens, max_completion_tokens,
                                           spend.input_per_m, spend.output_per_m)
        self.stopped_for_spend = False
        http_kwargs: dict[str, Any] = {"timeout": 90.0}
        if transport is not None:  # tests only
            http_kwargs["transport"] = transport
        self.client = AsyncOpenAI(http_client=ModelVersionHttpHook.async_client(version_log, **http_kwargs),
                                  max_retries=0, base_url=base_url)

    def _params(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        params: dict[str, Any] = {"model": self.model, "messages": messages,
                                  "response_format": {"type": "json_object"},
                                  "max_completion_tokens": self.max_completion_tokens}
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        return params

    async def one(self, request: JudgeRequest) -> dict[str, Any]:
        async with self.semaphore:
            reservation = None if self.stopped_for_spend else self.spend.reserve(self.worst_case)
            if reservation is None:
                self.stopped_for_spend = True
                return JudgeClient._result(request, None, None, {}, None, 0.0, "not_called_spend_cap")
            started = time.monotonic()
            try:
                response = await self.client.chat.completions.create(**self._params(request.messages))
            except Exception as exc:  # network/API errors -> missing; message text is not logged
                return JudgeClient._result(request, None, TypedAnswerParser.parse(QUESTION, None), {}, None,
                                           time.monotonic() - started,
                                           f"error:{type(exc).__name__}:{getattr(exc, 'status_code', '')}")
            usage = response.usage
            input_tokens = getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "completion_tokens", None)
            if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
                return JudgeClient._result(request, None, TypedAnswerParser.parse(QUESTION, None), {},
                                           response.model, time.monotonic() - started, "missing_usage")
            tokens = {"input": input_tokens, "output": output_tokens}
            details = getattr(usage, "completion_tokens_details", None)
            tokens["reasoning"] = getattr(details, "reasoning_tokens", 0) or 0 if details else 0
            self.spend.settle(reservation, tokens["input"], tokens["output"])
            content = response.choices[0].message.content if response.choices else None
            answer = TypedAnswerParser.parse(QUESTION, content)
            return JudgeClient._result(request, content, answer, tokens, response.model,
                                       time.monotonic() - started, response.choices[0].finish_reason if response.choices else None)

    @staticmethod
    def _result(request: JudgeRequest, content: str | None, answer: Any, tokens: dict[str, int],
                returned_model: str | None, latency: float, status: str | None) -> dict[str, Any]:
        missing = answer is None or isinstance(answer, MissingAnswer)
        return {"pair_id": request.pair_id, "arm": request.arm, "repeat": request.repeat,
                "probability": None if missing else answer.value,
                "missing_reason": (answer.check if answer is not None else f"not_called.{status}") if missing else None,
                "missing_detail": (answer.detail if answer is not None else status) if missing else None,
                "raw": content, "tokens": tokens, "returned_model": returned_model,
                "latency_s": round(latency, 3), "status": status}

    async def run(self, requests: list[JudgeRequest]) -> list[dict[str, Any]]:
        try:
            results = await asyncio.gather(*(self.one(r) for r in requests))
            for result in results:
                JsonlAppendLog.append(self.calls_path, result)
            return list(results)
        finally:
            await self.client.close()


class NameDisguiser:
    """Consistent random tokens for normalised names (disguised-names arm)."""

    @staticmethod
    def token_map(names: list[str | None], seed: int) -> dict[str, str]:
        rng = random.Random(seed)
        out: dict[str, str] = {}
        for name in sorted({n for n in names if n}):
            out[name] = f"Organization {rng.randrange(16**6):06X}"
        return out


class RunLog:
    @staticmethod
    def start(path: Path, run_id: str, **fields: Any) -> None:
        JsonlAppendLog.append(path, {"record_type": "run_start", "timestamp": UtcClock.iso(), "run_id": run_id, **fields})

    @staticmethod
    def event(path: Path, run_id: str, record_type: str, **fields: Any) -> None:
        JsonlAppendLog.append(path, {"record_type": record_type, "timestamp": UtcClock.iso(), "run_id": run_id, **fields})

    @staticmethod
    def dumps(value: Any) -> str:
        return json.dumps(value, sort_keys=True)
