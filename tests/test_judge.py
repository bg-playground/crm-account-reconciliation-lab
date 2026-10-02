import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from recon_lab.judge import JudgeClient, JudgeRequest, NameDisguiser, PromptBuilder, SpendLedger
from recon_lab.vendor.crashlab.model_version_log import ModelVersionLog
from recon_lab.vendor.crashlab.run_log import JsonlAppendLog

ROW_A = {"Id": "0015A0SECRETIDAAA", "Name": "Quorvane Systems Inc.", "Website": "quorvane.example",
         "Phone": "(415) 555-0101", "BillingStreet": "1 Main St", "BillingCity": "Springfield", "BillingState": "MA",
         "BillingPostalCode": "02110", "BillingCountry": "US", "Industry": "Technology", "NumberOfEmployees": "40",
         "OwnerId": "0055A0OWNERXXXXAAA", "CreatedDate": "2020-01-01T00:00:00.000Z",
         "LastModifiedDate": "2021-01-01T00:00:00.000Z", "Legacy_Account_Number__c": "LA-123456"}
ROW_B = dict(ROW_A, Id="0018g0OTHERIDXXAAA", Name="QUORVANE SYS", Website="", Legacy_Account_Number__c="BX-654321")


def ledger(tmp, phase="dev", cap=10.0, stop=15.0):
    return SpendLedger(Path(tmp) / "spend.jsonl", phase, input_per_m=0.10, output_per_m=0.50,
                       phase_cap_usd=cap, stop_at_total_usd=stop, hard_ceiling_usd=20.0)


class PromptBuilderTests(unittest.TestCase):
    def test_prompt_file_and_hash(self):
        system, template, sha = PromptBuilder.load()
        self.assertIn('"type": "yes_no"', system)
        self.assertIn("{record_1}", template)
        self.assertEqual(len(sha), 64)

    def test_no_ids_or_truth_leak(self):
        system, template, _ = PromptBuilder.load()
        msgs = PromptBuilder.messages(system, template, ROW_A, ROW_B, order_seed="x")
        text = json.dumps(msgs)
        for secret in ("SECRETID", "OTHERID", "OWNER", "LA-123456", "BX-654321", "2020-01-01", "entity", "E0"):
            self.assertNotIn(secret, text)
        self.assertIn("Website: (blank)", text)

    def test_order_is_deterministic_and_varies(self):
        system, template, _ = PromptBuilder.load()
        firsts = set()
        for i in range(20):
            msgs = PromptBuilder.messages(system, template, ROW_A, ROW_B, order_seed=str(i))
            self.assertEqual(msgs, PromptBuilder.messages(system, template, ROW_A, ROW_B, order_seed=str(i)))
            firsts.add(msgs[1]["content"].split("\n")[1])
        self.assertEqual(len(firsts), 2)

    def test_disguised_names(self):
        system, template, _ = PromptBuilder.load()
        tokens = NameDisguiser.token_map(["quorvane systems", "quorvane sys", None], seed=1)
        self.assertEqual(len(set(tokens.values())), 2)
        msgs = PromptBuilder.messages(system, template, ROW_A, ROW_B, order_seed="1",
                                      names=(tokens["quorvane systems"], tokens["quorvane sys"]))
        # Only the Name field is disguised; Website keeps its slug by design.
        self.assertNotIn("Name: Quorvane", msgs[1]["content"])
        self.assertNotIn("Name: QUORVANE", msgs[1]["content"])


class SpendLedgerTests(unittest.TestCase):
    def test_cost(self):
        self.assertAlmostEqual(SpendLedger.cost(1_000_000, 1_000_000, 0.10, 0.50), 0.60)
        self.assertAlmostEqual(SpendLedger.cost(500, 100, 0.10, 0.50), 0.0001)

    def test_phase_cap_and_stop_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            l = ledger(tmp, cap=1.0, stop=15.0)
            self.assertTrue(l.allow(0.5))
            l.add(9_000_000, 0)  # $0.90
            self.assertFalse(l.allow(0.2))
            l.flush("r1")
            # a later phase sees the cumulative total and stops before $15
            JsonlAppendLog.append(Path(tmp) / "spend.jsonl", {"record_type": "spend", "phase": "dev", "est_usd": 14.5})
            p = ledger(tmp, phase="published", cap=10.0, stop=15.0)
            self.assertAlmostEqual(p.prior_total, 15.4)
            self.assertFalse(p.allow(0.001))

    def test_hard_ceiling_caps_stop_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(SpendLedger(Path(tmp) / "s.jsonl", "dev", input_per_m=1, output_per_m=1, phase_cap_usd=50,
                                         stop_at_total_usd=30, hard_ceiling_usd=20).stop_at_total, 20)


class JudgeClientTests(unittest.TestCase):
    @staticmethod
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        marker = body["messages"][1]["content"]
        if "BAD" in marker:
            content = "not json"
        elif "FAIL" in marker:
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        else:
            content = json.dumps({"type": "yes_no", "probability": 0.9})
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "test-model-2026-01-01",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600}})

    def test_typed_results_versions_and_spend(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
            versions = ModelVersionLog(Path(tmp) / "v.jsonl", "r", policy="mark_invalid")
            spend = ledger(tmp)
            client = JudgeClient(model="test-model", version_log=versions, spend=spend, calls_path=Path(tmp) / "c.jsonl",
                                 max_completion_tokens=50, reasoning_effort="low",
                                 transport=httpx.MockTransport(self.handler), base_url="https://api.openai.invalid/v1")
            reqs = [JudgeRequest("p1", "main", 1, [{"role": "system", "content": "s"}, {"role": "user", "content": "ok"}]),
                    JudgeRequest("p2", "main", 1, [{"role": "system", "content": "s"}, {"role": "user", "content": "BAD"}]),
                    JudgeRequest("p3", "main", 1, [{"role": "system", "content": "s"}, {"role": "user", "content": "FAIL"}])]
            results = {r["pair_id"]: r for r in asyncio.run(client.run(reqs))}
            self.assertEqual(results["p1"]["probability"], 0.9)
            self.assertEqual(results["p1"]["returned_model"], "test-model-2026-01-01")
            self.assertEqual(results["p2"]["missing_reason"], "typed_answer.bad_json")
            self.assertTrue(results["p3"]["status"].startswith("error:BadRequestError"))
            self.assertIsNone(results["p3"]["probability"])
            self.assertEqual(spend.calls, 2)
            self.assertAlmostEqual(spend.phase_spend, 2 * 0.0001)
            summary = versions.summary()
            self.assertEqual(summary["calls"], 2)
            self.assertTrue(summary["run_valid"])
            log_text = (Path(tmp) / "c.jsonl").read_text() + (Path(tmp) / "v.jsonl").read_text()
            self.assertNotIn("test-not-a-key", log_text)

    def test_spend_cap_stops_calls(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": "test-not-a-key"}):
            versions = ModelVersionLog(Path(tmp) / "v.jsonl", "r", policy="mark_invalid")
            client = JudgeClient(model="m", version_log=versions, spend=ledger(tmp, cap=0.0), calls_path=Path(tmp) / "c.jsonl",
                                 max_completion_tokens=50, reasoning_effort=None,
                                 transport=httpx.MockTransport(self.handler), base_url="https://api.openai.invalid/v1")
            out = asyncio.run(client.run([JudgeRequest("p", "main", 1, [{"role": "user", "content": "ok"}])]))
            self.assertEqual(out[0]["status"], "not_called_spend_cap")
            self.assertTrue(client.stopped_for_spend)


if __name__ == "__main__":
    unittest.main()
