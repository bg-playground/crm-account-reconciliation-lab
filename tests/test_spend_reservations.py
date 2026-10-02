"""No-network budget tests with deliberately overlapping provider attempts."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from recon_lab.judge import JudgeClient, JudgeRequest, SpendLedger
from recon_lab.vendor.crashlab.model_version_log import ModelVersionLog


def make_ledger(tmp, cap=1.0, stop=15.0, ceiling=20.0):
    return SpendLedger(Path(tmp) / "spend.jsonl", "dev", input_per_m=0.10, output_per_m=0.50,
                       phase_cap_usd=cap, stop_at_total_usd=stop, hard_ceiling_usd=ceiling)


def request(index=0):
    return JudgeRequest(str(index), "main", 1, [{"role": "user", "content": "synthetic"}])


def response(usage=True):
    body = {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps({"type": "yes_no", "probability": 0.9})}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}
    return httpx.Response(200, json=body)


def client(tmp, spend, handler, concurrency=8):
    return JudgeClient(model="test-model", version_log=ModelVersionLog(Path(tmp) / "versions.jsonl", "test"),
                       spend=spend, calls_path=Path(tmp) / "calls.jsonl", max_completion_tokens=200,
                       reasoning_effort=None, concurrency=concurrency,
                       transport=httpx.MockTransport(handler), base_url="https://api.openai.invalid/v1")


class ReservationLedgerTests(unittest.TestCase):
    def test_phase_inclusive_total_and_hard_ceiling_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            spend = make_ledger(tmp, cap=0.5)
            token = spend.reserve(0.5)
            self.assertIsNotNone(token)
            self.assertIsNone(spend.reserve(0.01))
            spend.settle(token, 1_000_000, 0)
            self.assertAlmostEqual(spend.reserved_usd, 0)
            self.assertIsNotNone(spend.reserve(0.4))
            with self.assertRaises(ValueError):
                spend.settle(token, 0, 0)
        for stop, ceiling in [(0.5, 20), (30, 0.5)]:
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as tmp:
                spend = make_ledger(tmp, stop=stop, ceiling=ceiling)
                self.assertIsNone(spend.reserve(0.5))
                self.assertIsNotNone(spend.reserve(0.49))
                self.assertIsNone(spend.reserve(0.01))

    def test_uncertain_exposure_persists_without_becoming_actual_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            spend = make_ledger(tmp, cap=1)
            spend.reserve(0.9)
            row = spend.flush("uncertain")
            self.assertEqual(row["est_usd"], 0)
            self.assertEqual(row["unresolved_reserved_usd"], 0.9)
            self.assertEqual(row["unresolved_calls"], 1)
            resumed = make_ledger(tmp, cap=1)
            self.assertEqual(resumed.cumulative, 0)
            self.assertIsNone(resumed.reserve(0.2))
            other_phase = SpendLedger(spend.path, "published", input_per_m=0.1, output_per_m=0.5,
                                     phase_cap_usd=10, stop_at_total_usd=1, hard_ceiling_usd=20)
            self.assertIsNone(other_phase.reserve(0.2))

    def test_invalid_reservations_and_usage_do_not_free_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            spend = make_ledger(tmp)
            for amount in [-1, float("nan"), float("inf")]:
                with self.assertRaises(ValueError):
                    spend.reserve(amount)
            token = spend.reserve(0.5)
            with self.assertRaises(ValueError):
                spend.settle(token, -1, 0)
            self.assertEqual(spend.reserved_usd, 0.5)

    def test_usage_above_estimate_is_accounted_and_blocks_next_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            spend = make_ledger(tmp, cap=0.5)
            token = spend.reserve(0.1)
            spend.settle(token, 6_000_000, 0)
            self.assertAlmostEqual(spend.phase_spend, 0.6)
            self.assertIsNone(spend.reserve(0))


class PipelineReservationTests(unittest.TestCase):
    def test_pipeline_flushes_uncertain_exposure_on_cancellation(self):
        from recon_lab.pipeline import Paths, Rules, Steps

        async def cancelled_run(judge, requests):
            judge.spend.reserve(judge.worst_case)
            await judge.client.close()
            raise asyncio.CancelledError()

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
            spend_path = Path(tmp) / "spend.jsonl"
            def offline_client(**kwargs):
                return JudgeClient(**kwargs, transport=httpx.MockTransport(
                    lambda req: self.fail("Cancellation test must not call the provider")))

            with (patch.object(Paths, "spend", spend_path),
                  patch.object(JudgeClient, "run", cancelled_run),
                  patch("recon_lab.pipeline.JudgeClient", side_effect=offline_client)):
                with self.assertRaises(asyncio.CancelledError):
                    Steps.call_judge(Path(tmp), "cancelled", "dev", [request()], Rules.load(), None)
            rows = [json.loads(line) for line in spend_path.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["est_usd"], 0)
            self.assertEqual(rows[0]["unresolved_calls"], 1)
            self.assertGreater(rows[0]["unresolved_reserved_usd"], 0)


class ConcurrentJudgeTests(unittest.TestCase):
    def test_overlapping_requests_cannot_each_claim_same_remaining_budget(self):
        async def scenario(tmp, cap, stop):
            entered = 0
            two_in_flight = asyncio.Event()
            spend = make_ledger(tmp, cap=cap, stop=stop)

            async def handler(req):
                nonlocal entered
                entered += 1
                if entered == 2:
                    self.assertAlmostEqual(spend.reserved_usd, 0.00044)
                    two_in_flight.set()
                await asyncio.wait_for(two_in_flight.wait(), timeout=2)
                return response()

            judge = client(tmp, spend, handler)
            results = await judge.run([request(i) for i in range(8)])
            self.assertEqual(entered, 2)
            self.assertEqual(sum(r["status"] == "not_called_spend_cap" for r in results), 6)
            self.assertEqual(spend.calls, 2)
            self.assertAlmostEqual(spend.phase_spend, 0.0002)
            self.assertEqual(spend.reserved_usd, 0)
            self.assertEqual(len((Path(tmp) / "calls.jsonl").read_text().splitlines()), 8)

        for cap, stop in [(0.00044, 15), (1, 0.00045)]:
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as tmp, patch.dict(
                    os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
                asyncio.run(scenario(tmp, cap, stop))

    def test_settlement_reuses_unused_estimate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
            spend = make_ledger(tmp, cap=0.00032)
            judge = client(tmp, spend, lambda req: response(), concurrency=1)
            results = asyncio.run(judge.run([request(i) for i in range(3)]))
            self.assertEqual(spend.calls, 2)
            self.assertEqual(results[-1]["status"], "not_called_spend_cap")
            self.assertEqual(spend.reserved_usd, 0)

    def test_errors_missing_usage_and_retryable_errors_keep_reservation(self):
        for status in [400, 429, 500, "transport", "missing_usage"]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp, patch.dict(
                    os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
                entered = []

                def handler(req):
                    entered.append(req)
                    if status == "transport":
                        raise httpx.ReadError("synthetic", request=req)
                    if status == "missing_usage":
                        return response(usage=False)
                    return httpx.Response(status, json={"error": {"message": "synthetic"}})

                spend = make_ledger(tmp, cap=0.00022)
                judge = client(tmp, spend, handler, concurrency=1)
                results = asyncio.run(judge.run([request(0), request(1)]))
                self.assertEqual(len(entered), 1)  # SDK retries must not bypass reservation
                self.assertIsNone(results[0]["probability"])
                self.assertEqual(results[1]["status"], "not_called_spend_cap")
                self.assertEqual(spend.calls, 0)
                self.assertAlmostEqual(spend.reserved_usd, 0.00022)

    def test_cancellation_retains_exposure_and_closes_client(self):
        async def scenario(tmp):
            entered = asyncio.Event()
            hold = asyncio.Event()

            async def handler(req):
                entered.set()
                await hold.wait()
                return response()

            spend = make_ledger(tmp, cap=0.00022)
            judge = client(tmp, spend, handler)
            task = asyncio.create_task(judge.run([request()]))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertAlmostEqual(spend.reserved_usd, 0.00022)
            self.assertIsNone(spend.reserve(0.00022))
            self.assertTrue(judge.client.is_closed())

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
            asyncio.run(scenario(tmp))
