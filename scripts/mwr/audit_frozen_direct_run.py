"""Audit a completed Direct batch against the immutable requests and raw responses."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mwr.freeze_musique_rememr1 import DEFAULT_DEST, canonical, sha, write_json, write_jsonl
from scripts.mwr.run_frozen_musique_direct import parse_answer, read_jsonl, score_results, upstream_functions


def audit(bundle, run):
    requests = read_jsonl(bundle / 'requests/direct_requests.jsonl')
    inference = json.loads((bundle / 'inference/eval_musique_50.json').read_text())
    results = read_jsonl(run / 'ordered_results.jsonl')
    assert len(results) == len(requests) == len(inference)
    assert len({r['sample_id'] for r in results}) == len(results)
    assert len(list((run / 'trajectories').glob('*.json'))) == len(results)
    execution = json.loads((run / 'execution.json').read_text())
    assert execution['manifest_sha256'] == sha((bundle / 'metadata/frozen_manifest.json').read_bytes())
    assert json.loads((run / 'config.frozen.json').read_text()) == json.loads((bundle / 'config/direct.json').read_text())
    funcs = upstream_functions(bundle)
    models, finishes, http = Counter(), Counter(), Counter()
    usage = Counter()
    reasoning_count = 0
    index_rows = []
    for i, (request, result, inp) in enumerate(zip(requests, results, inference)):
        trace = json.loads((run / result['trajectory_path']).read_text())
        assert trace['sample_index'] == result['sample_index'] == i
        assert trace['sample_id'] == result['sample_id'] == request['sample_id'] == inp['id']
        assert trace['attempt'] == 1 and trace['status'] != 'IN_FLIGHT'
        assert trace['request']['json_body'] == request['json_body']
        assert trace['request_sha256'] == request['request_sha256'] == sha(canonical(request['json_body']))
        assert 'Authorization' not in trace['request']
        for key, value in result.items():
            assert trace[key] == value, (i, key)
        http[str(trace.get('http_status'))] += 1
        response = trace.get('raw_response') or {}
        if trace.get('http_status') == 200:
            pred, status = parse_answer(response, funcs)
            assert (pred, status) == (result['prediction'], result['status'])
            models[response.get('model', 'unspecified')] += 1
            message = response['choices'][0]['message']
            reasoning_count += bool(message.get('reasoning_content'))
        finishes[str(result.get('finish_reason'))] += 1
        counts = result.get('usage') or {}
        for field in ['prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens']:
            usage[field] += counts.get(field, 0) or 0
        usage['reasoning_tokens'] += (counts.get('completion_tokens_details') or {}).get('reasoning_tokens', 0) or 0
        index_rows.append({'sample_id': result['sample_id'], 'sample_index': i, 'type': inp['type'],
                           'question': inp['input'], 'status': result['status'], 'prediction': result['prediction'],
                           'trajectory': result['trajectory_path'], 'usage': counts})
    scored, summary = score_results(bundle, results)
    assert read_jsonl(run / 'scored.jsonl') == scored
    saved = json.loads((run / 'scores.json').read_text())
    for key, value in summary.items():
        assert saved[key] == value, key
    assert saved['complete'] is True and saved['attempted'] == len(results)
    by_hop = {}
    for hop in ['2hop', '3hop', '4hop']:
        selected = [s for s, inp in zip(scored, inference) if inp['type'].startswith(hop)]
        by_hop[hop] = {'n': len(selected),
                       'primary_correct': sum(s['upstream_primary_em'] for s in selected),
                       'alias_correct': sum(s['musique_alias_em'] for s in selected),
                       'alias_em_percent': round(100 * sum(s['musique_alias_em'] for s in selected) / len(selected), 4),
                       'alias_f1_percent': round(100 * sum(s['musique_alias_f1'] for s in selected) / len(selected), 4)}
    audit_report = {'complete': True, 'count': len(results), 'manifest_sha256': execution['manifest_sha256'],
                    'request_bodies_exactly_match_frozen': len(results), 'attempts_each': 1,
                    'response_parsing_rechecked': http.get('200', 0), 'scores_recomputed': True,
                    'actual_concurrency': execution['actual_concurrency'], 'frozen_concurrency': execution['frozen_concurrency'],
                    'response_models': dict(models), 'http_status_counts': dict(http),
                    'finish_reason_counts': dict(finishes), 'responses_with_reasoning_content': reasoning_count,
                    'token_usage': dict(usage), 'by_hop': by_hop,
                    'primary_correct': sum(s['upstream_primary_em'] for s in scored),
                    'alias_correct': sum(s['musique_alias_em'] for s in scored),
                    'elapsed_seconds': round((datetime.fromisoformat(saved['finished_utc']) - datetime.fromisoformat(saved['started_utc'])).total_seconds(), 2),
                    'median_request_seconds': round(statistics.median(r['latency_ms'] for r in results) / 1000, 2)}
    for item, score in zip(index_rows, scored):
        item.update({k: v for k, v in score.items() if k.endswith(('_em', '_f1'))})
    write_json(run / 'audit.json', audit_report)
    write_jsonl(run / 'index.jsonl', index_rows)
    return audit_report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=DEFAULT_DEST)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.bundle.resolve(), args.run.resolve()), ensure_ascii=False, indent=2))
