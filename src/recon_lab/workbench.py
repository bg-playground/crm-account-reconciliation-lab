"""Local, read-only evidence server for the synthetic review workbench."""

from __future__ import annotations

import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .decide import MERGES, REVIEWS, Cascade
from .pipeline import ROOT, Steps, Workbench
from .verify import verify

ASSETS = Path(__file__).with_name("workbench_assets")


def evidence(root: Path = ROOT) -> dict:
    """Fail closed on inconsistent evidence, then build a retrospective view."""
    audit = verify(root)
    published = json.loads((root / "results/metrics.json").read_text())
    protocol = json.loads((root / "config/frozen/protocol.json").read_text())
    bench = Workbench(protocol["rules"]["seeds"]["test"],
                      json.loads((root / "config/frozen/splink_model.json").read_text()),
                      data_root=root / "data")
    run = root / "runs" / audit["run_id"]
    calls = [json.loads(line) for line in (run / "calls.jsonl").read_text().splitlines()]
    main = Steps.ai_probabilities(calls)
    checks = {}
    for line in (run / "decisions.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("record_type") == "decision" and row["mode"] == "score":
            checks.setdefault(row["step"], {})[row["check"]] = row["outcome"]
    cut = protocol["cutoffs"]
    baseline = Cascade.baseline_decisions(bench.weights, cut["splink_merge_weight"])
    pairs = []
    # Include truth pairs missed by blocking so 'missed match' covers all 12 misses.
    for pair in sorted(bench.candidates | bench.truth):
        pid = Workbench.pair_id(pair)
        probabilities = main.get(pid, [])
        values = [v for v in probabilities if v is not None]
        mean = sum(values) / len(values) if values else None
        score = bench.scores.get(pair)
        recorded = checks.get(pid, {})
        if score is None:
            route = "not_blocked"
        elif score["mw"] >= cut["splink_merge_weight"]:
            route = "merge_splink"
        elif recorded.get("ai_merge") == "accept":
            route = "merge_ai"
        elif recorded.get("ai_nonmatch") == "accept":
            route = "nomatch_ai"
        elif recorded.get("ai_merge") == "missing":
            route = "review_ai_missing"
        elif recorded:
            route = "review_ai_uncertain"
        else:
            route = "review_not_judged"
        true = pair in bench.truth
        tags = []
        if route in REVIEWS:
            tags.append("review")
        if route in MERGES and not true:
            tags.append("false_merge")
        if true and route not in MERGES:
            tags.append("missed_match")
        # Opposing repeat routing bands, not just unequal numeric answers.
        bands = {"merge" if v >= cut["ai_merge"] else "no_match" if v <= cut["ai_nonmatch"]
                 else "review" for v in values}
        if len(bands) > 1:
            tags.append("disagreement")
        pairs.append({"id": pid, "left": {"org": pair[0].split(":")[0], **bench.by_uid[pair[0]]},
                      "right": {"org": pair[1].split(":")[0], **bench.by_uid[pair[1]]},
                      "route": route, "baseline": baseline.get(pair, "not_blocked"),
                      "score": score, "probabilities": probabilities, "mean": mean,
                      "truth": true, "tags": tags})
    return {"schema": "recon-workbench-evidence-v1", "run_id": audit["run_id"],
            "evidence_id": hashlib.sha256((root / "results/metrics.json").read_bytes()).hexdigest(),
            "verification": audit, "cutoffs": cut, "metrics": published["metrics"],
            "pairs": pairs}


def handler(data: dict):
    payload = json.dumps(data, allow_nan=False).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/evidence":
                body, mime = payload, "application/json"
            elif self.path in ("/", "/app.js", "/style.css"):
                name = "index.html" if self.path == "/" else self.path[1:]
                body = (ASSETS / name).read_bytes()
                mime = {"index.html": "text/html", "app.js": "text/javascript",
                        "style.css": "text/css"}[name]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def serve(port: int = 8765) -> None:
    print("Verifying committed evidence and preparing the workbench…", flush=True)
    data = evidence()
    with HTTPServer(("127.0.0.1", port), handler(data)) as server:
        host = server.server_address[0]
        print(f"Open http://{host}:{server.server_port} — synthetic evidence only; Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
