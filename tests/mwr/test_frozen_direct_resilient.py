import json
from pathlib import Path
from urllib.error import URLError

from scripts.mwr import execute_frozen_direct_resilient as batch


def prepared(monkeypatch, count=6):
    bundle = batch.DEFAULT_DEST
    config = json.loads((bundle / 'config/direct.json').read_text())
    requests = [json.loads(l) for l in (bundle / 'requests/direct_requests.jsonl').read_text().splitlines()][:count]
    monkeypatch.setattr(batch, 'verify', lambda _: (config, requests, {'manifest_sha256': 'offline-test'}))
    return bundle, requests


def test_transient_retries_preserve_payload_and_all_attempts(monkeypatch, tmp_path):
    bundle, requests = prepared(monkeypatch, 1)
    seen, delays = [], []

    def transport(payload):
        seen.append(payload)
        if len(seen) == 1:
            raise URLError('Connection refused')
        if len(seen) == 2:
            return 503, {'error': 'busy'}, {}
        return 200, {'choices': [{'message': {'content': 'Therefore, the answer is example.'}}]}, {}

    summary = batch.run_batch(bundle, tmp_path / 'run', 2, 'unused-secret', transport=transport, sleep=delays.append)
    assert delays == [15, 30]
    assert seen == [requests[0]['json_body']] * 3
    assert summary['all_questions_received_http200'] and summary['physical_attempts'] == 3
    attempts = sorted((tmp_path / 'run/attempts/0000').glob('*.json'))
    assert len(attempts) == 3
    assert [json.loads(p.read_text())['status'] for p in attempts] == ['TRANSPORT_ERROR', 'HTTP_ERROR', 'OK']
    assert all('unused-secret' not in p.read_text() for p in attempts)


def test_successful_generation_is_never_retried_for_bad_format(monkeypatch, tmp_path):
    bundle, _ = prepared(monkeypatch, 1)
    seen = []

    def transport(payload):
        seen.append(payload)
        return 200, {'choices': [{'message': {'content': 'Unformatted answer'}}]}, {}

    summary = batch.run_batch(bundle, tmp_path / 'run', 2, 'unused-secret', transport=transport, sleep=lambda _: None)
    assert len(seen) == 1
    assert summary['status_counts']['FORMAT_ERROR'] == 1
    assert summary['all_questions_received_http200']


def test_exhausted_network_failure_stops_other_questions(monkeypatch, tmp_path):
    bundle, requests = prepared(monkeypatch)
    seen = []

    def transport(payload):
        seen.append(payload)
        raise URLError('Connection refused')

    summary = batch.run_batch(bundle, tmp_path / 'run', 3, 'unused-secret', transport=transport,
                              max_attempts=2, sleep=lambda _: None)
    assert len(seen) == 2
    assert seen == [requests[0]['json_body']] * 2
    assert summary['aborted'] and not summary['complete']
    assert summary['attempted_questions'] == 1 and summary['denominator'] == 512
