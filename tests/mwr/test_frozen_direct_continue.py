import json

import pytest

from scripts.mwr import continue_frozen_direct as continuation
from scripts.mwr import execute_frozen_direct_resilient as batch


def source_run(monkeypatch, tmp_path):
    bundle = batch.DEFAULT_DEST
    config = json.loads((bundle / 'config/direct.json').read_text())
    requests = [json.loads(line) for line in (bundle / 'requests/direct_requests.jsonl').read_text().splitlines()][:4]
    prepared = lambda _: (config, requests, {'manifest_sha256': 'offline-test'})
    monkeypatch.setattr(batch, 'verify', prepared)
    monkeypatch.setattr(continuation, 'verify', prepared)
    responses = iter([
        (200, {'choices': [{'message': {'content': 'Therefore, the answer is example.'}}]}, {}),
        (200, {'choices': [{'message': {'content': 'Malformed answer without prefix'}}]}, {}),
        (503, {'error': 'busy'}, {}),
    ])
    source = tmp_path / 'source'
    batch.run_batch(bundle, source, 1, 'unused-secret', transport=lambda _: next(responses),
                    max_attempts=1, sleep=lambda _: None)
    return bundle, source, requests


def test_only_missing_or_failed_questions_run_and_source_is_preserved(monkeypatch, tmp_path):
    bundle, source, requests = source_run(monkeypatch, tmp_path)
    original = continuation.inventory(source)
    seen = []

    def transport(payload):
        seen.append(payload)
        return 200, {'choices': [{'message': {'content': 'Therefore, the answer is example.'}}]}, {}

    output = tmp_path / 'continued'
    summary = continuation.run_continuation(bundle, source, output, 2, 'unused-secret', transport=transport)
    assert seen == [r['json_body'] for r in requests[2:]]
    assert summary['complete'] and summary['remaining_questions'] == 0
    assert summary['reused_previous_predictions'] == 2 and summary['continued_questions'] == 2
    assert summary['denominator'] == 512 and summary['physical_attempts'] == 5
    rows = [json.loads(line) for line in (output / 'ordered_results.jsonl').read_text().splitlines()]
    assert len({r['sample_id'] for r in rows}) == 4
    assert rows[1]['status'] == 'FORMAT_ERROR'  # HTTP 200 was kept, despite malformed output.
    assert all((output / r['trajectory_path']).exists() for r in rows)
    assert continuation.inventory(source) == original
    assert continuation.inventory(output / 'source_snapshot') == original


@pytest.mark.parametrize('corruption', ['duplicate', 'request'])
def test_invalid_source_rejected_before_any_request(monkeypatch, tmp_path, corruption):
    bundle, source, _ = source_run(monkeypatch, tmp_path)
    if corruption == 'duplicate':
        path = source / 'results.jsonl'
        path.write_text(path.read_text() + path.read_text().splitlines()[0] + '\n')
    else:
        path = source / 'trajectories/0000.json'
        trace = json.loads(path.read_text())
        trace['json_body']['temperature'] = 0
        path.write_text(json.dumps(trace))
    seen = []
    with pytest.raises(ValueError):
        continuation.run_continuation(bundle, source, tmp_path / 'continued', 2, 'unused-secret',
                                      transport=seen.append)
    assert not seen


def test_exhausted_gate_keeps_prior_results_and_stops_new_submissions(monkeypatch, tmp_path):
    bundle, source, requests = source_run(monkeypatch, tmp_path)
    seen = []

    def transport(payload):
        seen.append(payload)
        return 503, {'error': 'busy'}, {}

    summary = continuation.run_continuation(bundle, source, tmp_path / 'continued', 2, 'unused-secret',
                                           transport=transport, max_attempts=2, sleep=lambda _: None)
    assert seen == [requests[2]['json_body']] * 2
    assert summary['aborted'] and not summary['complete']
    assert summary['reused_previous_predictions'] == 2 and summary['continued_questions'] == 1
    assert summary['remaining_questions'] == 2 and summary['denominator'] == 512


def test_continue_mode_finishes_queue_despite_failed_question(monkeypatch, tmp_path):
    bundle, source, requests = source_run(monkeypatch, tmp_path)
    seen = []

    def transport(payload):
        seen.append(payload)
        if payload == requests[3]['json_body']:
            raise TimeoutError('simulated timeout')
        return 200, {'choices': [{'message': {'content': 'Therefore, the answer is example.'}}]}, {}

    output = tmp_path / 'continued'
    summary = continuation.run_continuation(bundle, source, output, 1, 'unused-secret',
                                           transport=transport, max_attempts=2, sleep=lambda _: None,
                                           continue_on_failure=True)
    # Unseen index 3 runs before the previously failed index 2. Its failure
    # must not prevent index 2 from running, or alter any request payload.
    assert seen == [requests[3]['json_body']] * 2 + [requests[2]['json_body']]
    assert not summary['aborted'] and summary['coverage_complete']
    assert summary['remaining_questions'] == 1 and summary['continued_questions'] == 2
    remaining = json.loads((output / 'remaining.json').read_text())
    assert [row['sample_index'] for row in remaining['failed']] == [3]
    assert remaining['not_submitted'] == []


@pytest.mark.parametrize('http_status', [401, 403])
def test_continue_mode_stops_on_authentication_failure(monkeypatch, tmp_path, http_status):
    bundle, source, requests = source_run(monkeypatch, tmp_path)
    seen = []

    def transport(payload):
        seen.append(payload)
        return http_status, {'error': 'authentication failed'}, {}

    summary = continuation.run_continuation(bundle, source, tmp_path / 'continued', 1, 'unused-secret',
                                           transport=transport, continue_on_failure=True)
    assert seen == [requests[3]['json_body']]
    assert summary['aborted'] and summary['continued_questions'] == 1
    assert summary['stop_reason'] == 'authentication_or_client_error'


def test_continue_mode_does_not_wait_for_first_question_before_starting_second(monkeypatch, tmp_path):
    from threading import Event
    bundle, source, requests = source_run(monkeypatch, tmp_path)
    second_started = Event()

    def transport(payload):
        if payload == requests[3]['json_body']:
            assert second_started.wait(2), 'The first question incorrectly gated the pool'
        else:
            second_started.set()
        return 200, {'choices': [{'message': {'content': 'Therefore, the answer is example.'}}]}, {}

    summary = continuation.run_continuation(bundle, source, tmp_path / 'continued', 2, 'unused-secret',
                                           transport=transport, continue_on_failure=True)
    assert summary['complete'] and summary['continued_questions'] == 2
