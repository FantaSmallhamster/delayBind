"""Fresh Direct repeat with direct HTTPS, bounded transport retries and a circuit breaker.

The frozen request bodies are unchanged. A successful HTTP generation, including
an empty or malformed answer, is never retried. Every physical attempt is saved.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from threading import Event
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mwr.execute_frozen_direct_batch import atomic_json, now
from scripts.mwr.run_frozen_musique_direct import (
    DEFAULT_DEST, parse_answer, score_results, upstream_functions, verify, write_json, write_jsonl,
)

RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


def execute_one(index, request, output, functions, transport, key, max_attempts=4, sleep=time.sleep):
    """Keep the first HTTP 200 response; retries depend only on transport state."""
    folder = output / 'attempts' / f'{index:04d}'
    folder.mkdir(parents=True, exist_ok=True)
    attempts = []
    total_start = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        path = folder / f'{attempt:02d}.json'
        trace = {'sample_index': index, 'sample_id': request['sample_id'],
                 'request_sha256': request['request_sha256'], 'attempt': attempt,
                 'json_body': request['json_body'], 'started_utc': now(), 'status': 'IN_FLIGHT'}
        atomic_json(path, trace)
        begin = time.monotonic()
        retryable, retry_after = False, 0
        try:
            status, response, metadata = transport(request['json_body'])
            trace.update(http_status=status, raw_response=response, response_metadata=metadata)
            if status == 200:
                prediction, outcome = parse_answer(response, functions)
            else:
                prediction, outcome = None, 'HTTP_ERROR'
                retryable = status in RETRYABLE_HTTP
        except HTTPError as exc:
            prediction, outcome = None, 'HTTP_ERROR'
            retryable = exc.code in RETRYABLE_HTTP
            trace.update(http_status=exc.code, error_type=type(exc).__name__)
            try:
                body = exc.read().decode(errors='replace')[:4000]
                trace['error_body'] = body.replace(key, '[REDACTED]') if key else body
                retry_after = float(exc.headers.get('Retry-After', '0')) if exc.headers else 0
            except (ValueError, AttributeError, OSError):
                pass
        except (URLError, TimeoutError, ConnectionError, OSError) as exc:
            prediction, outcome, retryable = None, 'TRANSPORT_ERROR', True
            message = str(exc)
            trace.update(error_type=type(exc).__name__, error_message=message.replace(key, '[REDACTED]') if key else message)
            trace['provider_completion_unknown'] = True
        except Exception as exc:
            prediction, outcome = None, 'CLIENT_ERROR'
            trace.update(error_type=type(exc).__name__)
        trace.update(prediction=prediction, status=outcome, finished_utc=now(),
                     latency_ms=round((time.monotonic() - begin) * 1000, 2), retryable=retryable)
        attempts.append(str(path.relative_to(output)))
        will_retry = retryable and attempt < max_attempts
        trace['will_retry'] = will_retry
        trace['retry_delay_seconds'] = max(15 * 2 ** (attempt - 1), retry_after) if will_retry else 0
        atomic_json(path, trace)
        if not will_retry:
            break
        sleep(trace['retry_delay_seconds'])
    response = trace.get('raw_response')
    response = response if isinstance(response, dict) else {}
    choices = response.get('choices')
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    result = {'sample_index': index, 'sample_id': request['sample_id'], 'status': trace['status'],
              'prediction': trace['prediction'], 'http_status': trace.get('http_status'),
              'usage': response.get('usage'), 'finish_reason': choice.get('finish_reason'),
              'attempt_count': len(attempts), 'attempt_files': attempts,
              'latency_ms': round((time.monotonic() - total_start) * 1000, 2),
              'finished_utc': now(), 'trajectory_path': f'trajectories/{index:04d}.json'}
    final = {**trace, **result, 'request': {'json_body': request['json_body']},
             'selection_policy': 'first HTTP 200 response; no retry based on answer or correctness'}
    atomic_json(output / result['trajectory_path'], final)
    return result


def run_batch(bundle, output, concurrency, key, transport=None, max_attempts=4, sleep=time.sleep):
    if output.exists():
        raise ValueError('Output exists; use a new repeat directory')
    config, requests, preflight = verify(bundle)
    output.mkdir(parents=True)
    (output / 'trajectories').mkdir()
    write_json(output / 'preflight.json', preflight)
    write_json(output / 'config.frozen.json', config)
    started = now()
    execution = {'started_utc': started, 'bundle': str(bundle),
                 'manifest_sha256': preflight['manifest_sha256'], 'fresh_repeat': True,
                 'reused_previous_predictions': 0, 'request_body_changes': [],
                 'actual_concurrency': concurrency, 'frozen_concurrency': config['concurrency'],
                 'proxy_policy': 'ProxyHandler({}); direct HTTPS for this process only',
                 'max_attempts_per_question': max_attempts, 'semantic_retries': 0,
                 'retryable_http': sorted(RETRYABLE_HTTP), 'retry_delays_seconds': [15, 30, 60],
                 'retry_after_header_respected': True, 'first_success_only': True,
                 'transport_timeout_seconds': config['transport_timeout_seconds'],
                 'retry_disclosure': 'Timeout/disconnect may leave an unknown provider completion. All attempts are retained; failed-attempt token usage may be unavailable.',
                 'circuit_breaker': 'Stop submitting new questions after any exhausted/nonretryable transport/HTTP failure; drain already active requests.',
                 'pid': os.getpid(), 'credential_persisted': False,
                 'executor_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(output / 'execution.json', execution)
    shutil.copy2(Path(__file__), output / 'executor_snapshot.py')
    functions = upstream_functions(bundle)
    url = config['base_url'].rstrip('/') + '/chat/completions'

    def http_transport(payload):
        # Deliberately ignore system/environment proxies, retaining normal TLS verification.
        opener = build_opener(ProxyHandler({}))
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method='POST',
                          headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        with opener.open(request, timeout=config['transport_timeout_seconds']) as response:
            return response.status, json.loads(response.read().decode()), {
                'trace_id': response.getheader('x-siliconcloud-trace-id'),
                'request_id': response.getheader('x-request-id')}

    transport = transport or http_transport
    results, stop = [], Event()

    def one(index):
        result = execute_one(index, requests[index], output, functions, transport, key, max_attempts, sleep)
        if result.get('http_status') != 200:
            stop.set()
        return result

    def record(result):
        results.append(result)
        with (output / 'results.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps(result, ensure_ascii=False) + '\n')
        progress = {'completed': len(results), 'total': len(requests),
                    'status_counts': dict(Counter(r['status'] for r in results)),
                    'physical_attempts_completed': sum(r['attempt_count'] for r in results),
                    'last_sample_id': result['sample_id'], 'circuit_open': stop.is_set(), 'updated_utc': now()}
        atomic_json(output / 'progress.json', progress)
        print(json.dumps(progress, ensure_ascii=False), flush=True)

    print(json.dumps({'started': execution, 'preflight': preflight}), flush=True)
    record(one(0))
    if not stop.is_set():
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            indices = iter(range(1, len(requests)))
            running = set()
            for _ in range(concurrency):
                index = next(indices, None)
                if index is not None:
                    running.add(pool.submit(one, index))
            while running:
                done, running = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    record(future.result())
                    if not stop.is_set():
                        index = next(indices, None)
                        if index is not None:
                            running.add(pool.submit(one, index))
    results.sort(key=lambda r: r['sample_index'])
    write_jsonl(output / 'ordered_results.jsonl', results)
    scored, summary = score_results(bundle, results)
    summary.update(complete=len(results) == len(requests), started_utc=started, finished_utc=now(),
                   aborted=stop.is_set(), attempted_questions=len(results),
                   physical_attempts=sum(r['attempt_count'] for r in results),
                   all_questions_received_http200=len(results) == len(requests) and all(r.get('http_status') == 200 for r in results))
    write_jsonl(output / 'scored.jsonl', scored)
    write_json(output / 'scores.json', summary)
    print(json.dumps({'final': summary}, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=DEFAULT_DEST)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--key-stdin', action='store_true', help='Read a credential from an anonymous launch pipe')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(verify(args.bundle.resolve())[2], indent=2))
    else:
        if not 1 <= args.concurrency <= 8:
            parser.error('concurrency must be 1..8')
        if args.output.exists():
            parser.error('output already exists')
        key = sys.stdin.readline().strip() if args.key_stdin else os.environ.get('MODEL_API_KEY') or getpass.getpass('MODEL_API_KEY (hidden): ')
        if not key:
            parser.error('API key is empty')
        run_batch(args.bundle.resolve(), args.output.resolve(), args.concurrency, key)
