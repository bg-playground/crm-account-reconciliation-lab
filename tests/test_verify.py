"""Audit the real committed evidence and reject independent tampering."""

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recon_lab.cli import Cli
from recon_lab.pipeline import ROOT
from recon_lab.verify import EvidenceMismatch, verify

RUN = "runs/published-202-2fd9d168"


class OfflineVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for folder in ("data", "config", "results", "runs", "prompts"):
            shutil.copytree(ROOT / folder, self.root / folder)

    def hashes(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def edit_json(self, rel, edit):
        path = self.root / rel
        value = json.loads(path.read_text())
        edit(value)
        path.write_text(json.dumps(value))

    def edit_jsonl(self, rel, edit):
        path = self.root / rel
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        edit(rows)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_real_evidence_verifies_read_only_without_provider(self):
        before = self.hashes()
        with patch("recon_lab.judge.JudgeClient.__init__", side_effect=AssertionError("provider client forbidden")), \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            out = verify(self.root)
        self.assertEqual(out["verification"], "PASS")
        self.assertEqual(out["published_verdict"], "KILL")
        self.assertEqual(out["failed_criteria"], ["6_calibration"])
        self.assertEqual(out["calls_verified"], 3308)
        self.assertEqual(out["provider_calls"], 0)
        self.assertEqual(self.hashes(), before)

    def test_frozen_data_prompt_and_rules_tampering(self):
        for rel in ("data/synthetic/202/org_a/Account.csv", "data/ground_truth/101/entity_map.csv",
                    "prompts/account_match.v1.md", "config/protocol_rules.json"):
            with self.subTest(path=rel):
                path = self.root / rel
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(EvidenceMismatch, "frozen hash"):
                    verify(self.root)
                path.write_bytes(original)

    def test_raw_and_parsed_answer_disagreement(self):
        self.edit_jsonl(f"{RUN}/calls.jsonl", lambda rows: rows[0].update(probability=0.01))
        with self.assertRaisesRegex(EvidenceMismatch, "raw answer probability"):
            verify(self.root)

    def test_missing_and_duplicate_call_coverage(self):
        path = self.root / RUN / "calls.jsonl"
        original = path.read_bytes()
        self.edit_jsonl(f"{RUN}/calls.jsonl", lambda rows: rows.pop())
        with self.assertRaisesRegex(EvidenceMismatch, "judge call coverage"):
            verify(self.root)
        path.write_bytes(original)
        self.edit_jsonl(f"{RUN}/calls.jsonl", lambda rows: rows.append(rows[0]))
        with self.assertRaisesRegex(EvidenceMismatch, "duplicate judge call"):
            verify(self.root)

    def test_version_tampering(self):
        self.edit_jsonl(f"{RUN}/model_versions.jsonl", lambda rows: rows[0].update(returned_version="other-version"))
        with self.assertRaisesRegex(EvidenceMismatch, "model version"):
            verify(self.root)

    def test_spend_tampering(self):
        self.edit_jsonl("runs/spend_ledger.jsonl", lambda rows: rows[-1].update(est_usd=0.0))
        with self.assertRaisesRegex(EvidenceMismatch, "spend ledger: est_usd"):
            verify(self.root)

    def test_decision_tampering(self):
        self.edit_jsonl(f"{RUN}/decisions.jsonl", lambda rows: rows[1].update(outcome="accept"))
        with self.assertRaisesRegex(EvidenceMismatch, "decision ledger replay"):
            verify(self.root)

    def test_metric_and_verdict_tampering(self):
        path = self.root / "results/metrics.json"
        original = path.read_bytes()
        self.edit_json("results/metrics.json", lambda out: out["metrics"].update(review_queue_size=0))
        with self.assertRaisesRegex(EvidenceMismatch, "published metrics replay"):
            verify(self.root)
        path.write_bytes(original)
        self.edit_json("results/metrics.json", lambda out: out["verdict"].update(verdict="PASS"))
        with self.assertRaisesRegex(EvidenceMismatch, "frozen verdict replay"):
            verify(self.root)

    def test_review_and_report_tampering(self):
        for rel, message in (("results/review_queue.csv", "review queue replay"),
                             ("results/index.md", "results page replay"),
                             ("results/calibration.md", "calibration page replay")):
            with self.subTest(path=rel):
                path = self.root / rel
                original = path.read_bytes()
                path.write_bytes(original + b"tampered\n")
                with self.assertRaisesRegex(EvidenceMismatch, message):
                    verify(self.root)
                path.write_bytes(original)

    def test_cli_verification_failure_is_nonzero(self):
        with patch("recon_lab.verify.verify", side_effect=EvidenceMismatch("evidence mismatch")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(Cli.main(["verify"]), 1)
        self.assertIn("evidence mismatch", err.getvalue())


if __name__ == "__main__":
    unittest.main()
