"""Evidence coverage and read-only serving contracts for the local workbench."""

import hashlib
import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer

import pytest

from recon_lab.pipeline import ROOT
from recon_lab.workbench import evidence, handler


@pytest.fixture(scope="module")
def view():
    files = [p for directory in ("data", "config", "runs", "results")
             for p in (ROOT / directory).rglob("*") if p.is_file()]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    result = evidence()
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return result


def test_full_failure_coverage_and_published_counts(view):
    pairs = view["pairs"]
    assert len(pairs) == 1649  # 1648 candidates + one blocking miss
    assert len({p["id"] for p in pairs}) == len(pairs)
    assert sum(p["truth"] for p in pairs) == 353
    assert sum("false_merge" in p["tags"] for p in pairs) == 4
    assert sum("missed_match" in p["tags"] for p in pairs) == 12
    assert sum("review" in p["tags"] for p in pairs) == 21
    missed = [p for p in pairs if p["route"] == "not_blocked"]
    assert len(missed) == 1 and missed[0]["truth"]
    assert missed[0]["score"] is None and missed[0]["probabilities"] == []
    assert view["verification"]["published_verdict"] == "KILL"
    assert view["verification"]["provider_calls"] == 0


def test_audit_calls_do_not_override_splink_and_review_disagreement(view):
    by_id = {p["id"]: p for p in view["pairs"]}
    audit = by_id["org_a:0015A02NXXFwu1RQ2R|org_b:0018g0zXu3R7BIuAEN"]
    assert audit["route"] == "merge_splink"
    review = by_id["org_a:0015A0G0JzVIs2FQKT|org_b:0018g0OzrKJipDWASZ"]
    assert review["probabilities"] == [0.08, 0.68]
    assert review["mean"] == pytest.approx(0.38)
    assert review["route"] == "review_ai_uncertain"
    assert set(review["tags"]) == {"review", "disagreement"}
    for pair in view["pairs"]:
        values = [p for p in pair["probabilities"] if p is not None]
        bands = {"merge" if p >= 0.8 else "no_match" if p <= 0.3 else "review" for p in values}
        assert ("disagreement" in pair["tags"]) == (len(bands) > 1)


def test_missing_evidence_fails_closed(tmp_path):
    with pytest.raises(OSError):
        evidence(tmp_path)


def test_server_serves_only_explicit_assets_and_rejects_writes(view):
    with HTTPServer(("127.0.0.1", 0), handler(view)) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = HTTPConnection(*server.server_address)
            client.request("GET", "/api/evidence")
            response = client.getresponse()
            assert response.status == 200
            assert response.getheader("Cache-Control") == "no-store"
            assert json.loads(response.read())["evidence_id"] == view["evidence_id"]
            for path in ("/", "/app.js", "/style.css"):
                client.request("GET", path)
                response = client.getresponse()
                assert response.status == 200 and response.read()
            for path in ("/../.env", "/results/metrics.json", "/%2e%2e/.env"):
                client.request("GET", path)
                response = client.getresponse()
                assert response.status == 404
                response.read()
            client.request("POST", "/api/evidence", "{}")
            response = client.getresponse()
            assert response.status == 501
            response.read()
            client.close()
        finally:
            server.shutdown()
            worker.join()
