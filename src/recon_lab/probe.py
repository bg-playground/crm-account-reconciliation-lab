"""Tiny dev-seed probe: which request parameters does the model accept, and what does a call cost?"""

from __future__ import annotations

import random
from typing import Any

from .judge import RunLog


class Probe:
    EFFORTS = ("low", "minimal", None)

    @staticmethod
    def run(pairs: int) -> dict[str, Any]:
        from .pipeline import Paths, Rules, Steps, Workbench

        rules = Rules.load()
        seed = rules["seeds"]["dev"]
        bench = Workbench(seed)
        chosen = sorted(random.Random(7).sample(sorted(bench.candidates), pairs))
        plan = {"audit": [], "judged": chosen, "not_judged": [], "disguised": []}
        one_rep = {**rules, "model": {**rules["model"], "repeats_main_arm": 1}}
        report = {}
        for effort in Probe.EFFORTS:
            run_id = f"probe-{effort or 'none'}"
            run_dir = Paths.runs / "probe" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            RunLog.start(run_dir / "run_log.jsonl", run_id, phase="dev", reasoning_effort=effort, pairs=len(chosen))
            requests = Steps.build_requests(bench, plan, one_rep, seed)
            results, versions, ledger, _ = Steps.call_judge(run_dir, run_id, "dev", requests, one_rep, effort)
            report[run_id] = {"statuses": sorted({str(r["status"]) for r in results}),
                              "parsed": sum(r["probability"] is not None for r in results),
                              "tokens": [r["tokens"] for r in results],
                              "returned_models": sorted({str(r["returned_model"]) for r in results}),
                              "spend_usd": round(ledger.phase_spend, 6), "cumulative_usd": round(ledger.cumulative, 6),
                              "pinned_version": versions.get("pinned_version")}
        return report
