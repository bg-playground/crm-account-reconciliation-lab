"""Command line: python -m recon_lab.cli <generate|train-baseline|probe|dev|freeze|publish>."""

from __future__ import annotations

import argparse
import json
import os
import sys


class Cli:
    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        parser = argparse.ArgumentParser(prog="recon_lab")
        sub = parser.add_subparsers(dest="command", required=True)
        sub.add_parser("generate")
        sub.add_parser("train-baseline")
        probe = sub.add_parser("probe")
        probe.add_argument("--pairs", type=int, default=4)
        dev = sub.add_parser("dev")
        dev.add_argument("--reasoning-effort", default=None)
        sub.add_parser("freeze")
        sub.add_parser("publish")
        args = parser.parse_args(argv)
        from . import pipeline

        if args.command in ("probe", "dev", "publish") and not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
        if args.command == "generate":
            out = pipeline.Steps.generate()
        elif args.command == "train-baseline":
            out = pipeline.Steps.train_baseline()
        elif args.command == "probe":
            from .probe import Probe

            out = Probe.run(args.pairs)
        elif args.command == "dev":
            out = pipeline.DevRun.run(args.reasoning_effort)
            out = {"selection": out["selection"], "dev_in_sample": Cli.brief(out["metrics"])}
        elif args.command == "freeze":
            out = pipeline.Freezer.freeze()
        else:
            res = pipeline.PublishedRun.run()
            out = {"run_id": res["run_id"], "verdict": res["verdict"]["verdict"], "failed": res["verdict"]["failed"]}
        print(json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0

    @staticmethod
    def brief(m):
        if not m:
            return None
        return {k: m[k] for k in ("candidates", "true_pairs", "review_queue_size", "cascade_decisions")} | {
            "cascade_precision": m["cascade"]["precision"]["value"], "cascade_recall": m["cascade"]["recall"]["value"],
            "baseline_precision": m["baseline"]["precision"]["value"], "baseline_recall": m["baseline"]["recall"]["value"],
            "missing_rate": m["answers"]["missing_rate"], "returned_models": m["answers"]["returned_models"],
            "spend": m["published_spend_usd"], "tokens": m["tokens"]}


if __name__ == "__main__":
    raise SystemExit(Cli.main())
