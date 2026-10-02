"""End-to-end runs: train baseline (dev), dev run + cutoff selection, freeze, published run."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from .baseline import SplinkBaseline
from .blocking import Blocker, Pair
from .decide import (BASELINE_REVIEW_FLOOR_MW, MERGES, MERGE_AI, MERGE_SPLINK, REVIEW_MISSING,
                     REVIEW_NOT_JUDGED, REVIEWS, Cascade, CutoffSelector)
from .evaluate import BarChecker, CalibrationSummary, PairMetrics
from .generate import ORGS, AccountCsv, DatasetGenerator, GeneratorConfig
from .judge import JudgeClient, JudgeRequest, NameDisguiser, PromptBuilder, RunLog, SpendLedger
from .normalize import Normalizer
from .vendor.crashlab.model_version_log import ModelVersionLog
from .vendor.crashlab.run_mode import DecisionLedger, FrozenThresholds

ROOT = Path(__file__).resolve().parents[2]


class Paths:
    data = ROOT / "data"
    rules = ROOT / "config" / "protocol_rules.json"
    protocol = ROOT / "config" / "frozen" / "protocol.json"
    splink_model = ROOT / "config" / "frozen" / "splink_model.json"
    prompt = ROOT / "prompts" / "account_match.v1.md"
    runs = ROOT / "runs"
    spend = ROOT / "runs" / "spend_ledger.jsonl"
    results = ROOT / "results"


class Hasher:
    @staticmethod
    def file(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    @staticmethod
    def seed_data(seed: int) -> str:
        parts = [f"{org}/Account.csv:{Hasher.file(Paths.data / 'synthetic' / str(seed) / org / 'Account.csv')}" for org in ORGS]
        parts.append(f"entity_map.csv:{Hasher.file(Paths.data / 'ground_truth' / str(seed) / 'entity_map.csv')}")
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class Rules:
    @staticmethod
    def load() -> dict[str, Any]:
        return json.loads(Paths.rules.read_text())

    @staticmethod
    def weight_grid(rules: dict[str, Any]) -> list[float]:
        g = rules["cutoff_selection_on_dev"]["splink_merge_weight"]["grid"]
        n = int(round((g["stop"] - g["start"]) / g["step"]))
        return [round(g["start"] + i * g["step"], 6) for i in range(n + 1)]


class Workbench:
    """Loads one seed: rows, normalised records, candidates, truth, Splink scores."""

    def __init__(self, seed: int, model: dict | None = None, *, data_root: Path | None = None) -> None:
        self.seed = seed
        self.rows, self.truth_rows = AccountCsv.load_seed(Paths.data if data_root is None else data_root, seed)
        self.by_uid = {f"{org}:{r['Id']}": r for org in ORGS for r in self.rows[org]}
        self.records = [Normalizer.record(r, org) for org in ORGS for r in self.rows[org]]
        self.candidates, self.per_rule = Blocker.candidates(self.records)
        self.truth = Blocker.true_pairs(self.truth_rows)
        self.families = {f"{t['org']}:{t['Id']}": set(filter(None, t["families"].split("|"))) for t in self.truth_rows}
        self.scores: dict[Pair, dict[str, float]] = {}
        if model is not None:
            self.scores = Workbench.quiet(SplinkBaseline.predict, self.records, model)

    @staticmethod
    def quiet(fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    @property
    def weights(self) -> dict[Pair, float]:
        return {p: s["mw"] for p, s in self.scores.items()}

    @staticmethod
    def pair_id(pair: Pair) -> str:
        return f"{pair[0]}|{pair[1]}"

    @staticmethod
    def parse_pair(pair_id: str) -> Pair:
        left, right = pair_id.split("|")
        return (left, right)


class Steps:
    @staticmethod
    def generate() -> dict[str, Any]:
        rules = Rules.load()
        d = rules["data"]
        out = {}
        for name, seed in rules["seeds"].items():
            cfg = GeneratorConfig(seed=seed, rows_per_org=d["rows_per_org"], shared=d["shared_entities"],
                                  dups_per_org=d["intra_org_duplicates_per_org"], twins=d["hard_negative_twins"],
                                  unseen_rate=d[f"unseen_rate_{name}"])
            DatasetGenerator.write(DatasetGenerator.generate(cfg), Paths.data, seed)
            out[name] = {"seed": seed, "sha256": Hasher.seed_data(seed)}
        return out

    @staticmethod
    def train_baseline() -> dict[str, Any]:
        rules = Rules.load()
        dev = Workbench(rules["seeds"]["dev"])
        model = Workbench.quiet(SplinkBaseline.train, dev.records, rules["seeds"]["dev"])
        SplinkBaseline.save(model, Paths.splink_model)
        return {"splink_model_sha256": Hasher.file(Paths.splink_model)}

    @staticmethod
    def plan_judging(bench: Workbench, ts: float | None, rules: dict[str, Any], rng: random.Random) -> dict[str, Any]:
        m = rules["model"]
        above = sorted(p for p, w in bench.weights.items() if ts is not None and w >= ts)
        below = sorted(p for p, w in bench.weights.items() if ts is None or w < ts)
        audit = sorted(rng.sample(above, min(m["calibration_audit_pairs_above_splink_cutoff"], len(above))))
        room = m["max_judged_pairs"] - len(audit)
        judged = below if len(below) <= room else sorted(rng.sample(below, room))
        not_judged = sorted(set(below) - set(judged))
        disguised = sorted(rng.sample(judged, min(m["disguised_arm_pairs"], len(judged))))
        return {"audit": audit, "judged": judged, "not_judged": not_judged, "disguised": disguised}

    @staticmethod
    def build_requests(bench: Workbench, plan: dict[str, Any], rules: dict[str, Any], seed: int) -> list[JudgeRequest]:
        system, template, _ = PromptBuilder.load(Paths.prompt)
        names = NameDisguiser.token_map([Normalizer.name(r["Name"]) for r in bench.by_uid.values()], seed)
        requests = []
        for pair in plan["audit"] + plan["judged"]:
            left, right = bench.by_uid[pair[0]], bench.by_uid[pair[1]]
            for rep in range(1, rules["model"]["repeats_main_arm"] + 1):
                requests.append(JudgeRequest(Workbench.pair_id(pair), "main", rep, PromptBuilder.messages(
                    system, template, left, right, order_seed=f"{seed}:{pair}:{rep}")))
        for pair in plan["disguised"]:
            left, right = bench.by_uid[pair[0]], bench.by_uid[pair[1]]
            disguise = (names.get(Normalizer.name(left["Name"]), "Organization ?"),
                        names.get(Normalizer.name(right["Name"]), "Organization ?"))
            requests.append(JudgeRequest(Workbench.pair_id(pair), "disguised_names", 1, PromptBuilder.messages(
                system, template, left, right, order_seed=f"{seed}:{pair}:d", names=disguise)))
        return requests

    @staticmethod
    def call_judge(run_dir: Path, run_id: str, phase: str, requests: list[JudgeRequest], rules: dict[str, Any],
                   reasoning_effort: str | None) -> tuple[list[dict[str, Any]], dict[str, Any], SpendLedger, bool]:
        price, spend_rules, m = rules["pricing_usd_per_1m_tokens"], rules["spend"], rules["model"]
        ledger = SpendLedger(Paths.spend, phase, input_per_m=price["input"], output_per_m=price["output"],
                             phase_cap_usd=spend_rules[f"{'dev' if phase == 'dev' else 'published'}_cap_usd"],
                             stop_at_total_usd=spend_rules["stop_and_report_at_cumulative_usd"],
                             hard_ceiling_usd=spend_rules["hard_ceiling_usd"])
        versions = ModelVersionLog(run_dir / "model_versions.jsonl", run_id, policy="mark_invalid")
        client = JudgeClient(model=m["requested"], version_log=versions, spend=ledger, calls_path=run_dir / "calls.jsonl",
                             max_completion_tokens=m["max_completion_tokens"], reasoning_effort=reasoning_effort,
                             concurrency=m["concurrency"])
        results = asyncio.run(client.run(requests))
        version_summary = versions.write_summary()
        ledger.flush(run_id, note=f"{len(requests)} requests")
        return results, version_summary, ledger, client.stopped_for_spend

    @staticmethod
    def ai_probabilities(results: list[dict[str, Any]], arm: str = "main") -> dict[str, list[float | None]]:
        out: dict[str, list[float | None]] = {}
        for r in sorted(results, key=lambda r: (r["pair_id"], r["repeat"])):
            if r["arm"] == arm:
                out.setdefault(r["pair_id"], []).append(r["probability"])
        return out

    @staticmethod
    def cascade(bench: Workbench, ts: float | None, c_ai: float, l_ai: float, plan: dict[str, Any],
                main: dict[str, list[float | None]], run_dir: Path, run_id: str) -> dict[Pair, str]:
        checks = Cascade.checks(c_ai, l_ai)
        ledger_path = run_dir / "decisions.jsonl"
        shadow = DecisionLedger(ledger_path, f"{run_id}-shadow", "shadow", checks)
        judged = plan["judged"]
        for pair in judged:
            Cascade.ai_outcome(shadow, Workbench.pair_id(pair), Cascade.mean_answer(main.get(Workbench.pair_id(pair), [])))
        shadow.finish()
        score = DecisionLedger(ledger_path, f"{run_id}-score", "score", checks, shadow_run_id=f"{run_id}-shadow")
        decisions: dict[Pair, str] = {}
        for pair, weight in bench.weights.items():
            if ts is not None and weight >= ts:
                decisions[pair] = MERGE_SPLINK
        for pair in judged:
            decisions[pair] = Cascade.ai_outcome(score, Workbench.pair_id(pair), Cascade.mean_answer(main.get(Workbench.pair_id(pair), [])))
        for pair in plan["not_judged"]:
            decisions[pair] = REVIEW_NOT_JUDGED
        score.finish()
        return decisions

    @staticmethod
    def metrics(bench: Workbench, ts: float | None, c_ai: float, plan: dict[str, Any], decisions: dict[Pair, str],
                results: list[dict[str, Any]], version_summary: dict[str, Any], spend_usd: float,
                rules: dict[str, Any]) -> dict[str, Any]:
        truth = bench.truth
        base = Cascade.baseline_decisions(bench.weights, ts)
        b_merge = {p for p, d in base.items() if d == "merge"}
        b_review = {p for p, d in base.items() if d == "review"}
        c_merge = {p for p, d in decisions.items() if d in MERGES}
        c_review = {p for p, d in decisions.items() if d in REVIEWS}
        main = Steps.ai_probabilities(results, "main")
        disg = Steps.ai_probabilities(results, "disguised_names")
        rep1 = [(r["probability"], Workbench.parse_pair(r["pair_id"]) in truth) for r in results
                if r["arm"] == "main" and r["repeat"] == 1 and r["probability"] is not None]
        rep1_missing = sum(1 for r in results if r["arm"] == "main" and r["repeat"] == 1 and r["probability"] is None)
        calibration = CalibrationSummary.build(rep1, rep1_missing, rules["bar"]["calibration_min_count"])
        responded = [r for r in results if r["status"] is not None and not str(r["status"]).startswith(("error", "not_called"))]
        with_version = [r for r in responded if r["returned_model"]]
        missing = [r for r in results if r["probability"] is None]
        unseen_true = {p for p in truth if any("rebrand_domain" in bench.families.get(u, set()) for u in p)}
        dis_true = [p for p in plan["disguised"] if p in truth]
        main_r1 = {pid: v[0] for pid, v in main.items()}
        dis_hits = sum(1 for p in dis_true if (disg.get(Workbench.pair_id(p)) or [None])[0] is not None
                       and disg[Workbench.pair_id(p)][0] >= c_ai)
        main_hits = sum(1 for p in dis_true if main_r1.get(Workbench.pair_id(p)) is not None and main_r1[Workbench.pair_id(p)] >= c_ai)
        family_recall = {}
        for fam in sorted({f for fams in bench.families.values() for f in fams}):
            fam_true = {p for p in truth if any(fam in bench.families.get(u, set()) for u in p)}
            family_recall[fam] = {"cascade": PairMetrics.rate(len(fam_true & c_merge), len(fam_true)),
                                  "baseline": PairMetrics.rate(len(fam_true & b_merge), len(fam_true))}
        return {
            "seed": bench.seed,
            "candidates": len(bench.candidates),
            "true_pairs": len(truth),
            "blocking_per_rule": bench.per_rule,
            "blocker_matches_splink_pairs": set(bench.scores) == bench.candidates,
            "blocking_recall": PairMetrics.rate(len(bench.candidates & truth), len(truth)),
            "baseline": {**PairMetrics.confusion(b_merge, truth), "review_size": len(b_review),
                         "reachable_recall": len((b_merge | b_review) & truth) / len(truth),
                         "review_floor_match_weight": BASELINE_REVIEW_FLOOR_MW},
            "cascade": PairMetrics.confusion(c_merge, truth),
            "cascade_decisions": dict(Counter(decisions.values())),
            "cascade_reachable_recall": len((c_merge | c_review) & truth) / len(truth),
            "review_queue_size": len(c_review),
            "review_queue_true_pairs": len(c_review & truth),
            "ai_merges": PairMetrics.confusion({p for p, d in decisions.items() if d == MERGE_AI}, truth),
            "judged_pairs": len(plan["judged"]), "audit_pairs": len(plan["audit"]),
            "not_judged_pairs": len(plan["not_judged"]), "disguised_pairs": len(plan["disguised"]),
            "calibration": calibration,
            "answers": {"calls": len(results), "responded": len(responded), "missing": len(missing),
                        "missing_rate": len(missing) / len(results) if results else 1.0,
                        "missing_reasons": dict(Counter(r["missing_reason"] for r in missing)),
                        "version_coverage": len(with_version) / len(responded) if responded else 0.0,
                        "model_log_run_valid": bool(version_summary.get("run_valid")),
                        "model_log": version_summary,
                        "returned_models": dict(Counter(r["returned_model"] for r in responded)),
                        "both_repeats_missing_pairs": sum(1 for d in decisions.values() if d == REVIEW_MISSING)},
            "unseen_family_recall": PairMetrics.rate(len(unseen_true & c_merge), len(unseen_true)),
            "unseen_family_recall_baseline": PairMetrics.rate(len(unseen_true & b_merge), len(unseen_true)),
            "family_recall": family_recall,
            "disguised_names": {"true_pairs_in_sample": len(dis_true),
                                "recall_main_repeat1": main_hits / len(dis_true) if dis_true else None,
                                "recall_disguised": dis_hits / len(dis_true) if dis_true else None},
            "tokens": {"input": sum(r["tokens"].get("input", 0) for r in results),
                       "output": sum(r["tokens"].get("output", 0) for r in results),
                       "reasoning": sum(r["tokens"].get("reasoning", 0) for r in results)},
            "published_spend_usd": spend_usd,
        }


class Git:
    @staticmethod
    def run(*args: str) -> str:
        import subprocess

        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()

    @staticmethod
    def freeze_commit_pushed(path: Path) -> str:
        """Return the commit that last touched path, after checking it is clean and on origin/main."""

        rel = str(path.relative_to(ROOT))
        if Git.run("status", "--porcelain", "--", rel):
            raise RuntimeError(f"{rel} has uncommitted changes; commit and push the freeze first")
        sha = Git.run("log", "-1", "--format=%H", "--", rel)
        if not sha:
            raise RuntimeError(f"{rel} is not committed")
        Git.run("fetch", "--quiet", "origin")
        if "origin/main" not in Git.run("branch", "-r", "--contains", sha):
            raise RuntimeError("freeze commit is not on origin/main; push it first")
        return sha


class DevRun:
    @staticmethod
    def run(reasoning_effort: str | None) -> dict[str, Any]:
        rules = Rules.load()
        seed = rules["seeds"]["dev"]
        model = json.loads(Paths.splink_model.read_text())
        bench = Workbench(seed, model)
        sel = rules["cutoff_selection_on_dev"]
        ts = CutoffSelector.splink_merge_weight(bench.weights, bench.truth, Rules.weight_grid(rules),
                                                sel["splink_merge_weight"]["target_precision"])
        plan = Steps.plan_judging(bench, ts, rules, random.Random(seed + 1))
        requests = Steps.build_requests(bench, plan, rules, seed)
        run_id = "dev-" + RunLog.dumps(seed) + "-" + hashlib.sha256(RunLog.dumps([r.pair_id for r in requests]).encode()).hexdigest()[:8]
        run_dir = Paths.runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        _, _, prompt_sha = PromptBuilder.load(Paths.prompt)
        log = run_dir / "run_log.jsonl"
        RunLog.start(log, run_id, phase="dev", seed=seed, prompt_path="prompts/account_match.v1.md", prompt_sha256=prompt_sha,
                     requested_model=rules["model"]["requested"], reasoning_effort=reasoning_effort,
                     data_sha256=Hasher.seed_data(seed), splink_model_sha256=Hasher.file(Paths.splink_model),
                     rules_sha256=Hasher.file(Paths.rules), splink_merge_weight=ts, requests=len(requests))
        results, versions, ledger, stopped = Steps.call_judge(run_dir, run_id, "dev", requests, rules, reasoning_effort)
        main = Steps.ai_probabilities(results, "main")
        ai_p = {}
        for pair in plan["judged"]:
            answer = Cascade.mean_answer(main.get(Workbench.pair_id(pair), []))
            if not getattr(answer, "is_missing", True):
                ai_p[pair] = answer.value
        splink_merged = {p for p, w in bench.weights.items() if ts is not None and w >= ts}
        c_ai = CutoffSelector.ai_merge(ai_p, splink_merged, bench.truth, sel["ai_merge"]["grid"], sel["ai_merge"]["target_precision"])
        l_ai = CutoffSelector.ai_nonmatch(ai_p, bench.truth, len(bench.truth), sel["ai_nonmatch"]["grid"],
                                          sel["ai_nonmatch"]["max_true_pair_loss_share"])
        selection = {"dev_run_id": run_id, "splink_merge_weight": ts, "ai_merge": c_ai, "ai_nonmatch": l_ai,
                     "reasoning_effort": reasoning_effort, "stopped_for_spend": stopped,
                     "dev_spend_usd": round(ledger.phase_spend, 6), "cumulative_spend_usd": round(ledger.cumulative, 6)}
        metrics = None
        if c_ai is not None and l_ai is not None:
            decisions = Steps.cascade(bench, ts, c_ai, l_ai, plan, main, run_dir, run_id)
            metrics = Steps.metrics(bench, ts, c_ai, plan, decisions, results, versions, ledger.phase_spend, rules)
            (run_dir / "dev_metrics_in_sample.json").write_text(json.dumps(metrics, indent=2, sort_keys=True, default=str) + "\n")
        (Paths.runs / "dev_selection.json").write_text(json.dumps(selection, indent=2, sort_keys=True) + "\n")
        RunLog.event(log, run_id, "run_end", selection=selection, model_versions=versions)
        return {"selection": selection, "metrics": metrics}


class Freezer:
    @staticmethod
    def freeze() -> dict[str, Any]:
        rules = Rules.load()
        selection = json.loads((Paths.runs / "dev_selection.json").read_text())
        if selection["ai_merge"] is None or selection["ai_nonmatch"] is None:
            raise RuntimeError("dev selection found no admissible AI cutoff; nothing to freeze")
        from .vendor.crashlab.run_log import UtcClock

        protocol = {
            "schema": "recon-frozen-protocol-v1",
            "frozen_at_utc": UtcClock.iso(),
            "statement": "Bar, cutoffs, prompt, Splink model and test data are frozen here, before the published run on seed 202.",
            "rules": rules,
            "cutoffs": {"splink_merge_weight": selection["splink_merge_weight"], "ai_merge": selection["ai_merge"],
                        "ai_nonmatch": selection["ai_nonmatch"],
                        "baseline_review_floor_match_weight": BASELINE_REVIEW_FLOOR_MW},
            "reasoning_effort": selection["reasoning_effort"],
            "thresholds_fingerprint": FrozenThresholds.fingerprint(Cascade.checks(selection["ai_merge"], selection["ai_nonmatch"])),
            "dev_run_id": selection["dev_run_id"],
            "sha256": {"rules": Hasher.file(Paths.rules), "prompt": Hasher.file(Paths.prompt),
                       "splink_model": Hasher.file(Paths.splink_model),
                       "test_data_seed_202": Hasher.seed_data(rules["seeds"]["test"]),
                       "dev_data_seed_101": Hasher.seed_data(rules["seeds"]["dev"])},
        }
        Paths.protocol.parent.mkdir(parents=True, exist_ok=True)
        Paths.protocol.write_text(json.dumps(protocol, indent=2, sort_keys=True) + "\n")
        return protocol


class PublishedRun:
    @staticmethod
    def verify(protocol: dict[str, Any]) -> None:
        current = {"rules": Hasher.file(Paths.rules), "prompt": Hasher.file(Paths.prompt),
                   "splink_model": Hasher.file(Paths.splink_model),
                   "test_data_seed_202": Hasher.seed_data(protocol["rules"]["seeds"]["test"])}
        for key, value in current.items():
            if protocol["sha256"][key] != value:
                raise RuntimeError(f"{key} changed since the freeze; refusing to run")

    @staticmethod
    def run() -> dict[str, Any]:
        protocol = json.loads(Paths.protocol.read_text())
        freeze_sha = Git.freeze_commit_pushed(Paths.protocol)
        PublishedRun.verify(protocol)
        rules, cut = protocol["rules"], protocol["cutoffs"]
        seed = rules["seeds"]["test"]
        bench = Workbench(seed, json.loads(Paths.splink_model.read_text()))
        ts, c_ai, l_ai = cut["splink_merge_weight"], cut["ai_merge"], cut["ai_nonmatch"]
        plan = Steps.plan_judging(bench, ts, rules, random.Random(seed + 1))
        requests = Steps.build_requests(bench, plan, rules, seed)
        run_id = f"published-{seed}-{freeze_sha[:8]}"
        run_dir = Paths.runs / run_id
        if run_dir.exists():
            raise RuntimeError(f"{run_id} already exists; the published run is one-shot")
        run_dir.mkdir(parents=True)
        log = run_dir / "run_log.jsonl"
        _, _, prompt_sha = PromptBuilder.load(Paths.prompt)
        RunLog.start(log, run_id, phase="published", seed=seed, freeze_commit=freeze_sha,
                     protocol_sha256=Hasher.file(Paths.protocol), prompt_path="prompts/account_match.v1.md",
                     prompt_sha256=prompt_sha, requested_model=rules["model"]["requested"],
                     reasoning_effort=protocol["reasoning_effort"], thresholds_fingerprint=protocol["thresholds_fingerprint"],
                     cutoffs=cut, requests=len(requests))
        results, versions, ledger, stopped = Steps.call_judge(run_dir, run_id, "published", requests, rules,
                                                              protocol["reasoning_effort"])
        main = Steps.ai_probabilities(results, "main")
        decisions = Steps.cascade(bench, ts, c_ai, l_ai, plan, main, run_dir, run_id)
        metrics = Steps.metrics(bench, ts, c_ai, plan, decisions, results, versions, round(ledger.phase_spend, 6), rules)
        metrics["stopped_for_spend"] = stopped
        metrics["cumulative_spend_usd"] = round(ledger.cumulative, 6)
        verdict = BarChecker.check(metrics, rules["bar"])
        metrics["disguised_names"]["flagged"] = (
            metrics["disguised_names"]["recall_main_repeat1"] is not None and metrics["disguised_names"]["recall_disguised"] is not None
            and metrics["disguised_names"]["recall_main_repeat1"] - metrics["disguised_names"]["recall_disguised"] > rules["bar"]["disguised_names_flag_drop"])
        out = {"run_id": run_id, "freeze_commit": freeze_sha, "prompt_sha256": prompt_sha, "cutoffs": cut,
               "verdict": verdict, "metrics": metrics}
        Paths.results.mkdir(exist_ok=True)
        (Paths.results / "metrics.json").write_text(json.dumps(out, indent=2, sort_keys=True, default=str) + "\n")
        ReviewQueue.write(Paths.results / "review_queue.csv", bench, decisions, main)
        from .report import ResultsPage
        from .vendor.crashlab.calibration import CalibrationTable

        (Paths.results / "calibration.md").write_text(CalibrationTable.to_markdown(metrics["calibration"]["table"]) + "\n")
        (Paths.results / "index.md").write_text(ResultsPage.render(out, protocol))
        RunLog.event(log, run_id, "run_end", verdict=verdict["verdict"], failed=verdict["failed"],
                     spend_usd=round(ledger.phase_spend, 6), model_versions=versions)
        return out


class ReviewQueue:
    COLUMNS = ["pair_id", "reason", "left_org", "left_Id", "right_org", "right_Id", "left_Name", "right_Name",
               "left_Website", "right_Website", "left_Phone", "right_Phone", "left_BillingPostalCode",
               "right_BillingPostalCode", "splink_match_weight", "splink_probability", "ai_probabilities"]

    @staticmethod
    def write(path: Path, bench: Workbench, decisions: dict[Pair, str], main: dict[str, list[float | None]]) -> int:
        rows = []
        for pair, decision in sorted(decisions.items()):
            if decision not in REVIEWS:
                continue
            (lo, lid), (ro, rid) = pair[0].split(":"), pair[1].split(":")
            left, right = bench.by_uid[pair[0]], bench.by_uid[pair[1]]
            pid = Workbench.pair_id(pair)
            rows.append({"pair_id": pid, "reason": decision, "left_org": lo, "left_Id": lid, "right_org": ro, "right_Id": rid,
                         **{f"left_{k}": left[k] for k in ("Name", "Website", "Phone", "BillingPostalCode")},
                         **{f"right_{k}": right[k] for k in ("Name", "Website", "Phone", "BillingPostalCode")},
                         "splink_match_weight": round(bench.scores[pair]["mw"], 4),
                         "splink_probability": f"{bench.scores[pair]['p']:.6g}",
                         "ai_probabilities": ";".join("" if v is None else f"{v:.4g}" for v in main.get(pid, []))})
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=ReviewQueue.COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)
