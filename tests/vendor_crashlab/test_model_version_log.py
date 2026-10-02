from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from recon_lab.vendor.crashlab.model_version_log import (
    ModelVersionChanged,
    ModelVersionHttpHook,
    ModelVersionLog,
    ModelVersionMissing,
    ReturnedVersion,
    VersionChangePolicy,
)

FIXED = datetime(2026, 10, 1, 13, 40, tzinfo=timezone.utc)
FAKE_KEY = "test-not-a-real-key"


def completion(model: str | None, completion_id: str = "chatcmpl-1") -> dict:
    payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": 0,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    if model is not None:
        payload["model"] = model
    return payload


class LogCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "runs" / "model_calls.jsonl"
        ids = iter(f"call-{n}" for n in range(1, 100))
        self.call_ids = lambda: next(ids)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_log(self, policy=VersionChangePolicy.RESTART, **kwargs) -> ModelVersionLog:
        return ModelVersionLog(
            self.path,
            "run-1",
            policy=policy,
            clock=lambda: FIXED,
            call_id_factory=self.call_ids,
            **kwargs,
        )

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]


class VersionRecordingTests(LogCase):
    def test_records_returned_version_not_requested_family(self) -> None:
        log = self.make_log()
        record = log.record("gpt-5", "gpt-5-2025-08-07")
        self.assertEqual(record.requested_model, "gpt-5")
        self.assertEqual(record.returned_version, "gpt-5-2025-08-07")
        self.assertEqual(record.version_status, "baseline")
        self.assertEqual(log.pinned_version, "gpt-5-2025-08-07")

    def test_one_jsonl_record_per_call_with_required_fields(self) -> None:
        log = self.make_log()
        log.record("gpt-5", "gpt-5-2025-08-07")
        log.record("gpt-5", "gpt-5-2025-08-07")
        rows = self.lines()
        self.assertEqual(len(rows), 2)
        for row in rows:
            for field in ("timestamp", "call_id", "requested_model", "returned_version", "run_id"):
                self.assertIn(field, row)
            self.assertEqual(row["record_type"], "model_call")
            self.assertEqual(row["run_id"], "run-1")
            self.assertEqual(row["timestamp"], "2026-10-01T13:40:00+00:00")
        self.assertEqual([r["call_id"] for r in rows], ["call-1", "call-2"])
        self.assertEqual([r["version_status"] for r in rows], ["baseline", "match"])

    def test_log_is_append_only_across_instances(self) -> None:
        self.make_log().record("gpt-5", "v1")
        ModelVersionLog(self.path, "run-2", clock=lambda: FIXED).record("gpt-5", "v1")
        self.assertEqual([r["run_id"] for r in self.lines()], ["run-1", "run-2"])
        self.assertEqual(len(ModelVersionLog.read_records(self.path, run_id="run-2")), 1)

    def test_anthropic_style_response_object_uses_its_model_field(self) -> None:
        message = SimpleNamespace(model="claude-sonnet-4-5-20250929", content=[])
        self.assertEqual(ReturnedVersion.from_response(message), "claude-sonnet-4-5-20250929")
        self.assertIsNone(ReturnedVersion.from_response(SimpleNamespace()))

    def test_frozen_pin_is_enforced_from_the_first_call(self) -> None:
        log = self.make_log(policy=VersionChangePolicy.MARK_INVALID, pinned_version="v1")
        record = log.record("gpt-5", "v2")
        self.assertEqual(record.version_status, "changed")
        self.assertFalse(log.run_valid)


class ChangeDetectionTests(LogCase):
    def test_restart_policy_logs_then_raises_typed_exception(self) -> None:
        log = self.make_log(policy="restart")
        log.record("gpt-5", "v1")
        with self.assertRaises(ModelVersionChanged) as caught:
            log.record("gpt-5", "v2")
        self.assertEqual(caught.exception.record.returned_version, "v2")
        self.assertEqual(caught.exception.record.pinned_version, "v1")
        rows = self.lines()
        self.assertEqual(rows[-1]["version_status"], "changed")
        self.assertFalse(rows[-1]["run_valid"])
        self.assertTrue(log.restart_required)
        with self.assertRaises(ModelVersionChanged):
            log.raise_if_restart_required()

    def test_mark_invalid_policy_keeps_going_and_flags_log_and_summary(self) -> None:
        log = self.make_log(policy="mark_invalid")
        log.record("gpt-5", "v1")
        log.record("gpt-5", "v2")
        third = log.record("gpt-5", "v1")
        self.assertEqual(third.version_status, "match")
        self.assertFalse(third.run_valid, "run stays invalid after a change")
        self.assertFalse(log.restart_required)
        log.raise_if_restart_required()  # no-op under mark_invalid
        summary = log.write_summary()
        self.assertFalse(summary["run_valid"])
        self.assertEqual(summary["invalid_reasons"], ["model_version_changed"])
        self.assertEqual(summary["changed_call_ids"], ["call-2"])
        self.assertEqual(summary["versions_seen"], ["v1", "v2"])
        self.assertEqual(self.lines()[-1]["record_type"], "model_version_summary")
        self.assertFalse(self.lines()[-1]["run_valid"])

    def test_stable_run_is_valid(self) -> None:
        log = self.make_log()
        for _ in range(3):
            log.record("gpt-5", "v1")
        summary = log.write_summary()
        self.assertTrue(summary["run_valid"])
        self.assertEqual(summary["calls"], 3)
        self.assertEqual(summary["pinned_version"], "v1")

    def test_summary_recomputed_from_file_matches_in_memory(self) -> None:
        log = self.make_log(policy="mark_invalid")
        log.record("gpt-5", "v1")
        log.record("gpt-5", None)
        recomputed = ModelVersionLog.summarize_records(
            ModelVersionLog.read_records(self.path), run_id="run-1", policy="mark_invalid"
        )
        self.assertEqual(recomputed, log.summary())

    def test_run_with_no_calls_is_not_valid_in_summary(self) -> None:
        summary = self.make_log().summary()
        self.assertFalse(summary["run_valid"])
        self.assertEqual(summary["invalid_reasons"], ["no_model_calls_recorded"])


class MissingVersionTests(LogCase):
    def test_missing_version_never_matches_under_mark_invalid(self) -> None:
        log = self.make_log(policy="mark_invalid")
        log.record("gpt-5", "v1")
        for value in (None, "", "   ", 42):
            record = log.record("gpt-5", value)
            self.assertEqual(record.version_status, "missing")
            self.assertIsNone(record.returned_version)
        summary = log.summary()
        self.assertFalse(summary["run_valid"])
        self.assertEqual(len(summary["missing_call_ids"]), 4)
        self.assertIn("model_version_missing", summary["invalid_reasons"])

    def test_missing_first_version_does_not_become_the_pin(self) -> None:
        log = self.make_log(policy="mark_invalid")
        log.record("gpt-5", None)
        self.assertIsNone(log.pinned_version)
        self.assertEqual(log.record("gpt-5", "v1").version_status, "baseline")
        self.assertFalse(log.summary()["run_valid"])

    def test_missing_version_raises_under_restart(self) -> None:
        log = self.make_log(policy="restart")
        with self.assertRaises(ModelVersionMissing):
            log.record("gpt-5", None)
        self.assertEqual(self.lines()[0]["version_status"], "missing")


class HttpHookTests(LogCase, unittest.IsolatedAsyncioTestCase):
    def transport(self, responses: list[tuple[int, dict]]) -> httpx.MockTransport:
        queue = list(responses)
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            status, body = queue.pop(0)
            return httpx.Response(status, json=body)

        return httpx.MockTransport(handler)

    async def test_openai_sdk_calls_are_logged_with_returned_version(self) -> None:
        log = self.make_log(policy="mark_invalid")
        transport = self.transport([(200, completion("gpt-5-2025-08-07")), (200, completion("gpt-5-2026-01-15", "c2"))])
        async with ModelVersionHttpHook.async_client(log, transport=transport) as http:
            client = AsyncOpenAI(api_key=FAKE_KEY, base_url="https://llm.invalid/v1", http_client=http, max_retries=0)
            first = await client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hi"}])
            await client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(first.model, "gpt-5-2025-08-07")
        rows = self.lines()
        self.assertEqual([r["requested_model"] for r in rows], ["gpt-5", "gpt-5"])
        self.assertEqual([r["returned_version"] for r in rows], ["gpt-5-2025-08-07", "gpt-5-2026-01-15"])
        self.assertEqual([r["provider_call_id"] for r in rows], ["chatcmpl-1", "c2"])
        self.assertEqual(rows[1]["version_status"], "changed")

    async def test_hook_never_raises_into_sdk_under_restart(self) -> None:
        log = self.make_log(policy="restart")
        transport = self.transport([(200, completion("v1")), (200, completion("v2"))])
        async with ModelVersionHttpHook.async_client(log, transport=transport) as http:
            client = AsyncOpenAI(api_key=FAKE_KEY, base_url="https://llm.invalid/v1", http_client=http, max_retries=0)
            for _ in range(2):
                await client.chat.completions.create(model="gpt-5", messages=[{"role": "user", "content": "hi"}])
        self.assertTrue(log.restart_required)
        with self.assertRaises(ModelVersionChanged):
            log.raise_if_restart_required()

    async def test_failed_attempts_and_other_paths_are_not_counted(self) -> None:
        log = self.make_log()
        transport = self.transport([(500, {"error": "boom"}), (200, {"data": []}), (200, completion("v1"))])
        async with ModelVersionHttpHook.async_client(log, transport=transport) as http:
            await http.post("https://llm.invalid/v1/chat/completions", json={"model": "gpt-5"})
            await http.get("https://llm.invalid/v1/models")
            await http.post("https://llm.invalid/v1/chat/completions", json={"model": "gpt-5"})
        self.assertEqual([r["returned_version"] for r in self.lines()], ["v1"])

    async def test_response_without_model_field_is_logged_missing(self) -> None:
        log = self.make_log(policy="mark_invalid")
        transport = self.transport([(200, completion(None))])
        async with ModelVersionHttpHook.async_client(log, transport=transport) as http:
            await http.post("https://llm.invalid/v1/chat/completions", json={"model": "gpt-5"})
        self.assertEqual(self.lines()[0]["version_status"], "missing")
        self.assertFalse(log.run_valid)


if __name__ == "__main__":
    unittest.main()
