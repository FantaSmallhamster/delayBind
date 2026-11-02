"""Continue a frozen Direct run, preserving every existing HTTP 200 response.

The new directory contains a full source snapshot and a merged result journal.
Only absent questions or questions without an HTTP 200 response are submitted.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Event
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mwr import execute_frozen_direct_resilient as resilient
from scripts.mwr.execute_frozen_direct_batch import atomic_json, now
from scripts.mwr.run_frozen_musique_direct import (
    DEFAULT_DEST, parse_answer, score_results, upstream_functions, verify, write_json, write_jsonl,
)


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(directory):
    return {str(p.relative_to(directory)): file_hash(p)
            for p in sorted(directory.rglob('*')) if p.is_file()}


def source_plan(source, config, requests, preflight, functions):
    execution = json.loads((source / 'execution.json').read_text())
    if execution['manifest_sha256'] != preflight['manifest_sha256']:
        raise ValueError('Source frozen manifest mismatch')
    if json.loads((source / 'config.frozen.json').read_text()) != config:
        raise ValueError('Source frozen configuration mismatch')
    if not json.loads((source / 'scores.json').read_text()).get('finished_utc'):
        raise ValueError('Source run has not finalized')
    rows = [json.loads(line) for line in (source / 'results.jsonl').read_text().splitlines()]
    seen, preserved = set(), []
    for row in rows:
        index = row['sample_index']
        if not isinstance(index, int) or not 0 <= index < len(requests) or index in seen:
            raise ValueError('Duplicate or invalid source sample index')
        seen.add(index)
        request = requests[index]
        if row['sample_id'] != request['sample_id']:
            raise ValueError('Source sample ID/order mismatch')
        paths = [row['trajectory_path'], *row['attempt_files']]
        if row['attempt_count'] != len(row['attempt_files']):
            raise ValueError('Source attempt count mismatch')
        traces = []
        for relative in paths:
            path = (source / relative).resolve()
            if not path.is_relative_to(source.resolve()):
                raise ValueError('Source trace path escapes run directory')
            trace = json.loads(path.read_text())
            if (trace['sample_index'] != index or trace['sample_id'] != request['sample_id']
                    or trace['request_sha256'] != request['request_sha256']
                    or trace['json_body'] != request['json_body']):
                raise ValueError('Source trace/request mismatch')
            if trace['status'] == 'IN_FLIGHT':
                raise ValueError('Source contains unfinished attempts')
            traces.append(trace)
        for trace in [traces[0], traces[-1]]:
            for field in ['sample_id', 'status', 'prediction', 'http_status']:
                if trace.get(field) != row.get(field):
                    raise ValueError(f'Source result/trace mismatch: {field}')
        if row.get('http_status') == 200:
            prediction, status = parse_answer(traces[0].get('raw_response'), functions)
            if (prediction, status) != (row['prediction'], row['status']):
                raise ValueError('Source raw response/parsing mismatch')
            preserved.append({**row,
                              'trajectory_path': 'source_snapshot/' + row['trajectory_path'],
                              'attempt_files': ['source_snapshot/' + p for p in row['attempt_files']]})
        elif any(t.get('http_status') == 200 for t in traces[1:]):
            raise ValueError('Source discarded a successful HTTP generation')
    previous_attempts = list(source.rglob('attempts/*/*.json'))
    previously_attempted = set()
    for path in previous_attempts:
        trace = json.loads(path.read_text())
        previously_attempted.add(trace['sample_index'])
        if trace.get('status') == 'IN_FLIGHT':
            raise ValueError('Source has an orphan unfinished attempt')
    preserved.sort(key=lambda row: row['sample_index'])
    kept = {row['sample_index'] for row in preserved}
    pending = [i for i in range(len(requests)) if i not in kept]
    audit = {'source_run': str(source), 'source_rows': len(rows),
             'preserved_http200': len(preserved), 'pending': len(pending),
             'source_physical_attempts': len(previous_attempts),
             'previously_attempted_indices': sorted(previously_attempted),
             'pending_samples': [{'sample_index': i, 'sample_id': requests[i]['sample_id'],
                                  'request_sha256': requests[i]['request_sha256'],
                                  'reason': 'previous_failure' if i in seen else 'not_submitted'}
                                 for i in pending],
             'source_file_sha256': inventory(source)}
    return preserved, pending, audit


def run_continuation(bundle, source, output, concurrency, key, transport=None,
                     max_attempts=4, sleep=time.sleep, continue_on_failure=False):
    if output.exists():
        raise ValueError('Output exists; use a new continuation directory')
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError('Output must be outside the source run')
    config, requests, preflight = verify(bundle)
    functions = upstream_functions(bundle)
    results, pending, audit = source_plan(source, config, requests, preflight, functions)
    if continue_on_failure:
        # Previously failing requests must not prevent unseen questions from running.
        attempted = set(audit['previously_attempted_indices'])
        pending.sort(key=lambda index: (index in attempted, index))
    output.mkdir(parents=True)
    shutil.copytree(source, output / 'source_snapshot')
    if inventory(output / 'source_snapshot') != audit['source_file_sha256']:
        raise ValueError('Source changed during snapshot; refusing model calls')
    write_json(output / 'source_audit.json', audit)
    write_json(output / 'preflight.json', preflight)
    write_json(output / 'config.frozen.json', config)
    write_jsonl(output / 'results.jsonl', results)
    started = now()
    execution = {'started_utc': started, 'bundle': str(bundle), 'source_run': str(source),
                 'manifest_sha256': preflight['manifest_sha256'], 'fresh_repeat': False,
                 'reused_previous_predictions': len(results), 'pending_at_start': len(pending),
                 'selection_policy': 'Preserve every HTTP 200 response, irrespective of answer/format/correctness',
                 'request_body_changes': [], 'actual_concurrency': concurrency,
                 'frozen_concurrency': config['concurrency'],
                 'proxy_policy': 'ProxyHandler({}); direct HTTPS for this process only',
                 'max_attempts_per_pending_question': max_attempts, 'semantic_retries': 0,
                 'continue_on_failure': continue_on_failure,
                 'submission_indices': pending,
                 'submission_order': 'unattempted first, previous failures last' if continue_on_failure else 'original frozen order',
                 'retryable_http': sorted(resilient.RETRYABLE_HTTP),
                 'retry_delays_seconds': [15 * 2 ** i for i in range(max_attempts - 1)],
                 'retry_after_header_respected': True,
                 'transport_timeout_seconds': config['transport_timeout_seconds'],
                 'retry_disclosure': 'Timeout/disconnect may leave an unknown provider completion; all physical attempts retained.',
                 'circuit_breaker': ('Continue after individual transport/HTTP failures; stop on HTTP 401/403 or client errors; drain active requests.'
                                     if continue_on_failure else 'Stop submitting after exhausted/nonretryable failure; drain active requests.'),
                 'pid': os.getpid(), 'credential_persisted': False,
                 'executor_sha256': file_hash(Path(__file__)),
                 'attempt_executor_sha256': file_hash(Path(resilient.__file__))}
    write_json(output / 'execution.json', execution)
    shutil.copy2(__file__, output / 'executor_snapshot.py')
    shutil.copy2(resilient.__file__, output / 'attempt_executor_snapshot.py')
    url = config['base_url'].rstrip('/') + '/chat/completions'

    def http_transport(payload):
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method='POST',
                          headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(request, timeout=config['transport_timeout_seconds']) as response:
            return response.status, json.loads(response.read().decode()), {
                'trace_id': response.getheader('x-siliconcloud-trace-id'),
                'request_id': response.getheader('x-request-id')}

    transport = transport or http_transport
    stop, new_results = Event(), []
    stop_reason = None

    def progress():
        state = {'completed': len(results), 'total': len(requests),
                 'preserved_http200': audit['preserved_http200'],
                 'continued_completed': len(new_results), 'pending_at_start': len(pending),
                 'http200_total': sum(r.get('http_status') == 200 for r in results),
                 'status_counts': dict(Counter(r['status'] for r in results)),
                 'failed_questions': sum(r.get('http_status') != 200 for r in new_results),
                 'continue_on_failure': continue_on_failure,
                 'new_physical_attempts_completed': sum(r['attempt_count'] for r in new_results),
                 'circuit_open': stop.is_set(), 'stop_reason': stop_reason, 'updated_utc': now()}
        atomic_json(output / 'progress.json', state)
        print(json.dumps(state), flush=True)

    def one(index):
        nonlocal stop_reason
        result = resilient.execute_one(index, requests[index], output, functions, transport,
                                       key, max_attempts, sleep)
        fatal = result.get('http_status') in {401, 403} or result['status'] == 'CLIENT_ERROR'
        if fatal or (not continue_on_failure and result.get('http_status') != 200):
            stop_reason = 'authentication_or_client_error' if fatal else 'question_attempts_exhausted'
            stop.set()
        return result

    def record(result):
        results.append(result)
        new_results.append(result)
        with (output / 'results.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
        progress()

    print(json.dumps({'started': execution, 'preflight': preflight}), flush=True)
    progress()
    # Legacy mode gates the batch on one question. Continue mode starts the pool
    # immediately so a slow first question cannot hold back the entire batch.
    scheduled = pending
    if pending and not continue_on_failure:
        record(one(pending[0]))
        scheduled = pending[1:]
    if not stop.is_set():
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            indices, running = iter(scheduled), set()
            for _ in range(concurrency):
                index = next(indices, None)
                if index is not None:
                    running.add(pool.submit(one, index))
            while running:
                done, running = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    record(future.result())
                while not stop.is_set() and len(running) < concurrency:
                    index = next(indices, None)
                    if index is None:
                        break
                    running.add(pool.submit(one, index))
    results.sort(key=lambda row: row['sample_index'])
    write_jsonl(output / 'ordered_results.jsonl', results)
    scored, summary = score_results(bundle, results)
    http200 = sum(r.get('http_status') == 200 for r in results)
    failed = [r for r in results if r.get('http_status') != 200]
    recorded = {r['sample_index'] for r in results}
    missing = [i for i in range(len(requests)) if i not in recorded]
    write_json(output / 'remaining.json', {
        'failed': [{'sample_index': r['sample_index'], 'sample_id': r['sample_id'],
                    'status': r['status'], 'http_status': r.get('http_status')} for r in failed],
        'not_submitted': [{'sample_index': i, 'sample_id': requests[i]['sample_id']} for i in missing]})
    summary.update(complete=http200 == len(requests), all_questions_received_http200=http200 == len(requests),
                   coverage_complete=len(results) == len(requests), aborted=stop.is_set(),
                   continue_on_failure=continue_on_failure, stop_reason=stop_reason,
                   remaining_questions=len(requests) - http200,
                   started_utc=started, finished_utc=now(),
                   reused_previous_predictions=audit['preserved_http200'],
                   continued_questions=len(new_results),
                   physical_attempts=audit['source_physical_attempts'] + sum(r['attempt_count'] for r in new_results),
                   source_snapshot_verified=inventory(output / 'source_snapshot') == audit['source_file_sha256'])
    write_jsonl(output / 'scored.jsonl', scored)
    write_json(output / 'scores.json', summary)
    print(json.dumps({'final': summary}), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=DEFAULT_DEST)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--continue-on-failure', action='store_true',
                        help='Run unseen questions first and continue after individual failures; still stop on authentication/client errors')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--key-stdin', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 8:
        parser.error('concurrency must be 1..8')
    if args.output.exists():
        parser.error('output already exists')
    if not args.execute:
        config, requests, preflight = verify(args.bundle.resolve())
        _, _, audit = source_plan(args.source.resolve(), config, requests, preflight,
                                  upstream_functions(args.bundle.resolve()))
        print(json.dumps({k: v for k, v in audit.items() if k not in {'source_file_sha256', 'pending_samples', 'previously_attempted_indices'}}, indent=2))
    else:
        key = sys.stdin.readline().strip() if args.key_stdin else os.environ.get('MODEL_API_KEY') or getpass.getpass('MODEL_API_KEY (hidden): ')
        if not key:
            parser.error('API key is empty')
        run_continuation(args.bundle.resolve(), args.source.resolve(), args.output.resolve(), args.concurrency, key,
                         continue_on_failure=args.continue_on_failure)
