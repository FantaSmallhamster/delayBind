"""Rebuild the registered development grid report without model calls."""
import json
import hashlib
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.mwr.analysis import paired_bootstrap
from delaybind_core.mwr.experiments import retention_replay
from delaybind_core.cli import _load_tokenizer

REPORT = ROOT / 'reports/mwr/phase2_20261028'


def read_rows(path):
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]


def main():
    registration = json.loads((REPORT / 'pilot.registration.json').read_text())
    groups, conditions, comparisons, all_calls, direct_requests, replay_inputs = [], [], [], [], defaultdict(list), []
    for condition in registration['configurations']:
        config = json.loads(Path(condition['path']).read_text())
        path = Path(config['output_dir']); capacity = condition['live_state_tokens']
        rows = read_rows(path / 'results.jsonl')
        expected = {(sid, method, 'original') for sid in condition['sample_ids'] for method in config['methods']}
        actual = {(r['sample_id'], r['method'], r['order']) for r in rows}
        if actual != expected or len(rows) != len(expected):
            raise ValueError('Incomplete/duplicated grid at ' + str(path))
        db = sqlite3.connect(path / 'experiment.sqlite')
        events = defaultdict(list)
        for rid, typ, payload in db.execute('select run_id,event_type,payload_json from runtime_events order by event_seq'):
            events[rid].append((typ, json.loads(payload)))
        calls = [json.loads(r[0]) for r in db.execute('select payload_json from model_call_observations')]
        db.close()
        all_calls.extend(calls)
        for row in rows:
            ev = events[row['run_id']]
            committed = [p for t,p in ev if t == 'WINDOW_COMMITTED']
            evicted = [p for t,p in ev if t == 'EVIDENCE_EVICTED']
            retained = [p for t,p in ev if t == 'EVIDENCE_RETAINED']
            c = {'capacity': capacity, 'sample_id': row['sample_id'], 'method': row['method'],
                'status': row['status'], 'answer_exact': row['answer_exact'],
                'answer_token_f1': row['answer_token_f1'], 'error': row.get('error'),
                'max_committed_state_tokens': max((p['live_state_tokens'] for p in committed), default=0),
                'max_before_retention_tokens': max((p['before_tokens'] for p in retained + evicted), default=0),
                'eviction_events': len(evicted), 'windows_committed': len(committed),
                'known_input_tokens': row['known_input_tokens'], 'known_output_tokens': row['known_output_tokens'],
                'physical_attempts': row['physical_attempts'], 'cache_hits': row['cache_hits'],
                'usage_unknown_attempts': row['usage_unknown_attempts'], 'trajectory_path': row['trajectory_path']}
            c['budget_pressure'] = bool(evicted) or c['max_before_retention_tokens'] > capacity
            conditions.append(c)
            if row['method'] == 'direct_mwr':
                initial = next((call for call in calls if call['run_id'] == row['run_id'] and call['interface'] == 'ANSWER' and not call['cache_hit']), None)
                if initial:
                    payload_hash = hashlib.sha256(json.dumps(initial['raw_request'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    direct_requests[row['sample_id']].append({'capacity': capacity, 'payload_sha256': payload_hash,
                        'status': row['status'], 'prediction': row.get('prediction'), 'answer_exact': row['answer_exact']})
            elif capacity == 8192:
                plan = next((p['plan'] for t,p in ev if t == 'PLAN_CREATED'), None)
                proposals = [p['evidence'] for t,p in ev if t == 'EVIDENCE_PROPOSED']
                if plan is not None:
                    count = max([p['window'] for p in committed] + [e['observed_window'] for e in proposals] + [0])
                    windows = [[e for e in proposals if e['observed_window'] == w] for w in range(1,count+1)]
                    replay_inputs.append({'sample_id': row['sample_id'], 'method': row['method'], 'plan': plan,
                        'windows': windows, 'run_status': row['status'], 'committed_windows': len(committed)})
        for method in config['methods']:
            selected = [c for c in conditions if c['capacity'] == capacity and c['method'] == method]
            groups.append({'capacity': capacity, 'method': method, 'count': len(selected),
                'correct': sum(c['answer_exact'] for c in selected),
                'em': sum(c['answer_exact'] for c in selected) / len(selected),
                'f1': sum(c['answer_token_f1'] for c in selected) / len(selected),
                'status_counts': dict(Counter(c['status'] for c in selected)),
                'pressure_conditions': sum(c['budget_pressure'] for c in selected),
                'eviction_events': sum(c['eviction_events'] for c in selected),
                'max_committed_state_tokens': max(c['max_committed_state_tokens'] for c in selected),
                'max_before_retention_tokens': max(c['max_before_retention_tokens'] for c in selected),
                'physical_attempts': sum(c['physical_attempts'] for c in selected),
                'known_input_tokens': sum(c['known_input_tokens'] for c in selected),
                'known_output_tokens': sum(c['known_output_tokens'] for c in selected)})
        for other in ('flat_memory_mwr', 'direct_mwr'):
            comparisons.append({'capacity': capacity, 'method_a': 'delaybind_mwr', 'method_b': other,
                                **paired_bootstrap(rows, 'delaybind_mwr', other)})
    tokenizer = _load_tokenizer('models/Qwen3.5-9B-tokenizer/tokenizer.json')
    replay = []
    for stream in replay_inputs:
        outcome = retention_replay(stream['plan'], stream['windows'], tokenizer, (2048,4096,8192))
        replay.append({**stream, 'replay': outcome})
    (REPORT / 'pilot_fixed_candidate_replay.json').write_text(json.dumps({'diagnostic_only': True,
        'scope': 'Shared predicted proposal stream from the 8k runs, without bindings; measures retention alone. Failed runs remain labeled; not independent extraction or answer comparison.', 'streams': replay}, ensure_ascii=False, indent=2) + '\n')
    for name in ('prompt_ab','semantic_holdout','cue_interference','grounding_probe'):
        report = json.loads((REPORT / (name + '.json')).read_text())
        for row in report['rows']:
            # Reconcile against the physical request ledger, not outcome counters.
            db = sqlite3.connect(REPORT / (name + '.sqlite'))
            all_calls.extend(json.loads(r[0]) for r in db.execute('select payload_json from model_call_observations where run_id=?',(row['run_id'],)))
            db.close()
    postfix_path = ROOT / 'results/mwr/direct_sourcefix_20261028'
    postfix_rows = read_rows(postfix_path / 'results.jsonl')
    if len(postfix_rows) != 16 or {r['sample_id'] for r in postfix_rows} != set(registration['configurations'][0]['sample_ids']):
        raise ValueError('Incomplete Direct source-fix engineering verification')
    db = sqlite3.connect(postfix_path / 'experiment.sqlite')
    all_calls.extend(json.loads(r[0]) for r in db.execute('select payload_json from model_call_observations'))
    db.close()
    unique = {(c['run_id'],c['call_id']): c for c in all_calls}
    if len(unique) != len(all_calls):
        raise ValueError('duplicated usage ledger')
    physical = [c for c in all_calls if not c['cache_hit']]
    usage = {'physical_attempts': len(physical), 'cache_hits': sum(c['cache_hit'] for c in all_calls),
        'failed_attempts': sum(c.get('error') is not None for c in physical),
        'unknown_usage_attempts': sum(c.get('input_tokens') is None or c.get('output_tokens') is None for c in physical),
        'known_input_tokens': sum(c.get('input_tokens') or 0 for c in physical),
        'known_output_tokens': sum(c.get('output_tokens') or 0 for c in physical)}
    report = {'date': '2026-10-28', 'development_only': True, 'pilot_conditions': len(conditions),
        'independent_questions': 16, 'groups': groups, 'paired_development_comparisons': comparisons,
        'conditions': conditions, 'direct_repeated_request_comparison': dict(direct_requests),
        'postfix_engineering_verification': {'count': len(postfix_rows), 'correct': sum(r['answer_exact'] for r in postfix_rows),
            'status_counts': dict(Counter(r['status'] for r in postfix_rows)),
            'scope': 'Separate 16-question Direct engineering verification after source segmentation/citation repair changes; not merged into frozen capacity grid.',
            'source_hash': json.loads((postfix_path / 'config.resolved.json').read_text())['source_hash']},
        'usage_including_all_development_probes': usage,
        'formal_512_model_calls': 0,
        'formal_gate': 'NOT_PASSED: semantic direction errors persist; source visibility alone is not independent delayed evidence annotation.'}
    (REPORT / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'groups': groups, 'usage': usage}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
