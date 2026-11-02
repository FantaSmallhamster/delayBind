import json
from threading import Lock
import time

from scripts.mwr import execute_frozen_direct_batch as batch


def prepared(monkeypatch):
    bundle = batch.DEFAULT_DEST
    config = json.loads((bundle / "config/direct.json").read_text())
    requests = [json.loads(l) for l in (bundle / "requests/direct_requests.jsonl").read_text().splitlines()][:9]
    monkeypatch.setattr(batch, "verify", lambda _: (config, requests, {"manifest_sha256": "offline-test"}))
    return bundle, requests


def test_scheduler_sends_each_exact_request_once_and_respects_concurrency(monkeypatch, tmp_path):
    bundle, requests = prepared(monkeypatch)
    lock = Lock()
    seen, active, maximum = [], 0, 0

    def transport(payload):
        nonlocal active, maximum
        with lock:
            seen.append(payload)
            active += 1
            maximum = max(maximum, active)
        time.sleep(.01)
        with lock:
            active -= 1
        return 200, {"choices": [{"message": {"content": "Therefore, the answer is example."}}]}, {}

    summary = batch.run_batch(bundle, tmp_path / "run", 3, "unused-secret", transport)
    assert len(seen) == len(requests) == 9
    assert {json.dumps(p, sort_keys=True) for p in seen} == {json.dumps(r["json_body"], sort_keys=True) for r in requests}
    assert 1 < maximum <= 3
    assert summary["complete"] and summary["attempted"] == 9
    traces = list((tmp_path / "run/trajectories").glob("*.json"))
    assert len(traces) == 9
    assert all("unused-secret" not in p.read_text() for p in traces)


def test_auth_failure_at_gate_stops_further_submission(monkeypatch, tmp_path):
    bundle, _ = prepared(monkeypatch)
    seen = []

    def transport(payload):
        seen.append(payload)
        return 401, {"error": "invalid credential"}, {}

    summary = batch.run_batch(bundle, tmp_path / "run", 3, "unused-secret", transport)
    assert len(seen) == 1
    assert not summary["complete"] and summary["aborted"]
    assert summary["denominator"] == 512
