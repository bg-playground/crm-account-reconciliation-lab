"""Read-only offline replay of the committed v1 evidence; no provider calls."""

from __future__ import annotations

import hashlib
import json
import random
import tempfile
from collections import Counter
from pathlib import Path

from .decide import BASELINE_REVIEW_FLOOR_MW, Cascade
from .evaluate import BarChecker
from .judge import QUESTION, SpendLedger
from .pipeline import ROOT, ReviewQueue, Steps, Workbench
from .report import ResultsPage
from .vendor.crashlab.calibration import CalibrationTable
from .vendor.crashlab.model_version_log import ModelVersionLog
from .vendor.crashlab.run_log import JsonlAppendLog
from .vendor.crashlab.run_mode import FrozenThresholds
from .vendor.crashlab.typed_answers import TypedAnswerParser


class EvidenceMismatch(ValueError):
    """Committed evidence cannot be reproduced consistently."""


def require(ok: bool, label: str) -> None:
    if not ok:
        raise EvidenceMismatch(label)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def same(actual, expected, label: str) -> None:
    # JSON canonicalization also normalizes tuples in recomputed values.
    require(json.dumps(actual, sort_keys=True, allow_nan=False)
            == json.dumps(expected, sort_keys=True, allow_nan=False), label)


def verify(root: Path = ROOT) -> dict:
    root = Path(root)
    protocol_path = root / "config/frozen/protocol.json"
    protocol = json.loads(protocol_path.read_text())
    rules, cut = protocol["rules"], protocol["cutoffs"]
    same(protocol["thresholds_fingerprint"],
         FrozenThresholds.fingerprint(Cascade.checks(cut["ai_merge"], cut["ai_nonmatch"])),
         "frozen threshold fingerprint")
    same(cut["baseline_review_floor_match_weight"], BASELINE_REVIEW_FLOOR_MW, "baseline review floor")
    for key, rel in (("rules", "config/protocol_rules.json"), ("prompt", "prompts/account_match.v1.md"),
                     ("splink_model", "config/frozen/splink_model.json")):
        same(digest(root / rel), protocol["sha256"][key], f"frozen hash: {key}")
    same(json.loads((root / "config/protocol_rules.json").read_text()), rules, "frozen rules")
    for phase, seed in rules["seeds"].items():
        parts = [f"{org}/Account.csv:{digest(root / 'data/synthetic' / str(seed) / org / 'Account.csv')}"
                 for org in ("org_a", "org_b")]
        parts.append(f"entity_map.csv:{digest(root / 'data/ground_truth' / str(seed) / 'entity_map.csv')}")
        same(hashlib.sha256("\n".join(parts).encode()).hexdigest(),
             protocol["sha256"][f"{phase}_data_seed_{seed}"], f"frozen hash: {phase} data")

    published = json.loads((root / "results/metrics.json").read_text())
    run_id = published["run_id"]
    require(run_id == f"published-{rules['seeds']['test']}-{published['freeze_commit'][:8]}", "run identity")
    run_dir = root / "runs" / run_id
    events = JsonlAppendLog.read(run_dir / "run_log.jsonl")
    require(len(events) == 2, "published run must have one start and one end")
    start, end = events
    same((start["record_type"], end["record_type"]), ("run_start", "run_end"), "run lifecycle")
    for event in events:
        same(event["run_id"], run_id, "run log identity")
    same(start["protocol_sha256"], digest(protocol_path), "run protocol hash")
    same(start["freeze_commit"], published["freeze_commit"], "freeze identity")
    same(start["cutoffs"], cut, "run cutoffs")
    same(published["cutoffs"], cut, "published cutoffs")
    for key, value in (("seed", rules["seeds"]["test"]), ("phase", "published"),
                       ("requested_model", rules["model"]["requested"]),
                       ("reasoning_effort", protocol["reasoning_effort"]),
                       ("thresholds_fingerprint", protocol["thresholds_fingerprint"]),
                       ("prompt_sha256", protocol["sha256"]["prompt"])):
        same(start[key], value, f"run start: {key}")
    same(published["prompt_sha256"], protocol["sha256"]["prompt"], "published prompt hash")

    bench = Workbench(rules["seeds"]["test"],
                      json.loads((root / "config/frozen/splink_model.json").read_text()), data_root=root / "data")
    require(set(bench.scores) == bench.candidates, "blocking and Splink pair coverage")
    plan = Steps.plan_judging(bench, cut["splink_merge_weight"], rules, random.Random(bench.seed + 1))
    expected_calls = {(Workbench.pair_id(p), "main", repeat)
                      for p in plan["audit"] + plan["judged"]
                      for repeat in range(1, rules["model"]["repeats_main_arm"] + 1)}
    expected_calls |= {(Workbench.pair_id(p), "disguised_names", 1) for p in plan["disguised"]}
    calls = JsonlAppendLog.read(run_dir / "calls.jsonl")
    identities = [(c["pair_id"], c["arm"], c["repeat"]) for c in calls]
    require(len(identities) == len(set(identities)), "duplicate judge call")
    same(set(identities) == expected_calls, True, "judge call coverage")
    same(start["requests"], len(calls), "planned request count")
    for call in calls:
        parsed = TypedAnswerParser.parse(QUESTION, call["raw"])
        same(call["probability"], None if parsed.is_missing else parsed.value, "raw answer probability")
        if call["raw"] is not None or call["status"] == "stop":
            same(call["missing_reason"], parsed.check if parsed.is_missing else None, "raw answer missing reason")
        for value in call["tokens"].values():
            require(type(value) is int and value >= 0, "nonnegative token usage")

    version_rows = JsonlAppendLog.read(run_dir / "model_versions.jsonl")
    versions = ModelVersionLog.read_records(run_dir / "model_versions.jsonl")
    require(len({r["call_id"] for r in versions}) == len(versions), "duplicate model version call")
    pinned, valid = None, True
    for row in versions:
        returned = row["returned_version"]
        status = "missing" if not returned else "baseline" if pinned is None else "match" if returned == pinned else "changed"
        if returned and pinned is None:
            pinned = returned
        valid = valid and status not in ("missing", "changed")
        for key, value in (("run_id", run_id), ("requested_model", rules["model"]["requested"]),
                           ("version_status", status), ("pinned_version", pinned),
                           ("run_valid", valid), ("policy", "mark_invalid")):
            same(row[key], value, f"model version: {key}")
    responded = [c for c in calls if c["status"] is not None
                 and not str(c["status"]).startswith(("error", "not_called"))]
    same(Counter(c["returned_model"] for c in responded),
         Counter(r["returned_version"] for r in versions), "returned model coverage")
    summary = ModelVersionLog.summarize_records(versions, run_id=run_id, policy="mark_invalid")
    summaries = [r for r in version_rows if r["record_type"] == "model_version_summary"]
    require(len(summaries) == 1, "model version summary count")
    same({k: summaries[0][k] for k in summary}, summary, "model version summary")
    same(end["model_versions"], summary, "run end model summary")

    price = rules["pricing_usd_per_1m_tokens"]
    spend = round(sum(SpendLedger.cost(c["tokens"].get("input", 0), c["tokens"].get("output", 0),
                                      price["input"], price["output"]) for c in calls), 6)
    spend_rows = [r for r in JsonlAppendLog.read(root / "runs/spend_ledger.jsonl") if r["record_type"] == "spend"]
    current = [r for r in spend_rows if r["run_id"] == run_id]
    require(len(current) == 1 and spend_rows[-1] == current[0], "published spend ledger entry")
    for key, value in (("est_usd", spend), ("phase", "published"), ("calls", len(responded)),
                       ("input_tokens", sum(c["tokens"].get("input", 0) for c in calls)),
                       ("output_tokens", sum(c["tokens"].get("output", 0) for c in calls)),
                       ("price_input_per_m", price["input"]), ("price_output_per_m", price["output"])):
        same(current[0][key], value, f"spend ledger: {key}")
    cumulative = round(sum(r["est_usd"] for r in spend_rows), 6)
    same(current[0]["cumulative_est_usd"], cumulative, "cumulative spend ledger")
    same(end["spend_usd"], spend, "run end spend")

    with tempfile.TemporaryDirectory(prefix="recon-offline-") as tmp:
        temporary = Path(tmp)
        main = Steps.ai_probabilities(calls)
        decisions = Steps.cascade(bench, cut["splink_merge_weight"], cut["ai_merge"], cut["ai_nonmatch"],
                                  plan, main, temporary, run_id)
        strip_time = lambda rows: [dict((k, v) for k, v in r.items() if k != "timestamp") for r in rows]
        canonical = lambda rows: sorted(json.dumps(r, sort_keys=True) for r in strip_time(rows))
        same(canonical(JsonlAppendLog.read(temporary / "decisions.jsonl")),
             canonical(JsonlAppendLog.read(run_dir / "decisions.jsonl")), "decision ledger replay")
        metrics = Steps.metrics(bench, cut["splink_merge_weight"], cut["ai_merge"], plan, decisions,
                                calls, summary, spend, rules)
        metrics["stopped_for_spend"] = any(c["status"] == "not_called_spend_cap" for c in calls)
        metrics["cumulative_spend_usd"] = cumulative
        dis = metrics["disguised_names"]
        dis["flagged"] = (dis["recall_main_repeat1"] is not None and dis["recall_disguised"] is not None
                          and dis["recall_main_repeat1"] - dis["recall_disguised"] > rules["bar"]["disguised_names_flag_drop"])
        same(metrics, published["metrics"], "published metrics replay")
        verdict = BarChecker.check(metrics, rules["bar"])
        same(verdict, published["verdict"], "frozen verdict replay")
        same(end["verdict"], verdict["verdict"], "run end verdict")
        same(end["failed"], verdict["failed"], "run end failed criteria")
        ReviewQueue.write(temporary / "review_queue.csv", bench, decisions, main)
        require((temporary / "review_queue.csv").read_bytes() == (root / "results/review_queue.csv").read_bytes(),
                "review queue replay")
        same(ResultsPage.render({**published, "metrics": metrics, "verdict": verdict}, protocol),
             (root / "results/index.md").read_text(), "results page replay")
        same(CalibrationTable.to_markdown(metrics["calibration"]["table"]) + "\n",
             (root / "results/calibration.md").read_text(), "calibration page replay")
    return {"verification": "PASS", "run_id": run_id, "published_verdict": verdict["verdict"],
            "failed_criteria": verdict["failed"], "calls_verified": len(calls), "provider_calls": 0}
