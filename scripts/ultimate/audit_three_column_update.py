"""Validate a complete three-column UPDATE run against the retained V04 run."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.fact_protocol import parse_update
from delaybind_core.metrics import answer_exact, answer_token_f1
from delaybind_core.schema import QueryPlan, RuntimeEvent
from delaybind_core.subqueries import initial_subquery_state, replay_subqueries


def read_json(path):
    return json.loads(path.read_text())


def percentile(values, proportion):
    values = sorted(values)
    index = (len(values) - 1) * proportion
    lo = int(index)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)


def audit_run(directory, *, candidate, json_update=False):
    rows = [json.loads(line) for line in (directory / 'results.jsonl').read_text().splitlines()]
    assert len(rows) == len({r['sample_id'] for r in rows}) == 128
    manifest = read_json(directory / 'code_manifest.json')
    assert set(manifest['sample_ids']) == {r['sample_id'] for r in rows}
    if candidate:
        assert all(hashlib.sha256((ROOT / n).read_bytes()).hexdigest() == h
                   for n, h in manifest['source_sha256'].items()), 'Source changed since evaluation started'
    interfaces, events, update_protocol = defaultdict(Counter), Counter(), Counter()
    violations, errors, replayed = [], [], 0
    for row in rows:
        assert row['answer_exact'] == answer_exact(row['prediction'], row['gold_answers'])
        assert abs(row['answer_token_f1'] - answer_token_f1(row['prediction'], row['gold_answers'])) < 1e-12
        assert (row['method'], row['order']) == ('v5_predicted', 'original')
        trace = read_json(directory / 'trajectories' / Path(row['trajectory_path']).name)
        for call in trace['model_calls']:
            interface, counts = call['interface'], interfaces[call['interface']]
            counts['records'] += 1
            counts['cache_hits'] += bool(call['cache_hit'])
            if not call['cache_hit']:
                counts['network_attempts'] += 1
                counts['retries'] += call['attempt'] > 1
                counts['failed_attempts'] += bool(call.get('error'))
                counts['input_tokens'] += call.get('input_tokens') or 0
                counts['output_tokens'] += call.get('output_tokens') or 0
            expected = 'HIGH' if interface in {'PLAN', 'MEMORY_GROUNDED'} else 'LOW'
            if interface not in {'PLAN', 'UPDATE', 'MEMORY_GROUNDED', 'RECALL', 'ANSWER'} or call['agent_role'] != expected:
                violations.append({'sample_id': row['sample_id'], 'interface': interface, 'role': call['agent_role']})
            if candidate and interface == 'UPDATE' and call.get('parsed_output') is not None:
                update_protocol['responses'] += 1
                try:
                    if json_update:
                        from delaybind_core.update_json import parse_update_json
                        parsed = parse_update_json(call['parsed_output'])
                    else:
                        parsed = parse_update(call['parsed_output'], recover=True)
                    update_protocol['parsed_fact_rows'] += len(parsed.facts)
                    update_protocol['hint_rows'] += len(parsed.hints)
                    update_protocol['rejected_rows'] += len(parsed.rejected_lines)
                    update_protocol['responses_without_rejected_rows'] += not parsed.rejected_lines and not parsed.bindings
                    update_protocol['forbidden_bindings'] += len(parsed.bindings)
                except ValueError:
                    update_protocol['unparseable_responses'] += 1
        states = {}
        for e in trace['events']:
            kind, payload = e['event_type'], e['payload']
            events[kind] += 1
            if kind == 'PLAN_CREATED':
                initial = initial_subquery_state(QueryPlan.model_validate(payload['plan']))
                states = {qid: execution.model_dump(mode='json') for qid, execution in initial.executions.items()}
            if kind == 'QUERY_STATE_UPDATED':
                states[payload['query_id']] = payload['state']
            if kind == 'AGENT_CALLED':
                interfaces[payload['interface']]['logical_calls'] += 1
            if candidate and kind in {'FACT_COMMITTED', 'FACT_DEFERRED'} and payload.get('reason') != 'PLAN_REROUTE':
                qid = payload['use']['query_id']
                live = states[qid]['status']
                wanted = 'FACT_DEFERRED' if live == 'DORMANT' else 'FACT_COMMITTED'
                if kind != wanted or payload.get('routing_source') != 'QUERY_STATE' or payload.get('query_status') != live:
                    violations.append({'sample_id': row['sample_id'], 'event': e['event_seq'], 'reason': 'routing state mismatch'})
            if kind == 'MEMORY_VERIFY_COMPLETED' and payload.get('mode') != 'joint_source_memory':
                violations.append({'sample_id': row['sample_id'], 'event': kind, 'mode': payload.get('mode')})
            if kind.startswith('RAW_FALLBACK') or any(s in kind for s in ('EVIDENCE_RESCUE', 'SOFT_RESCUE', 'SOURCE_ADJUDICATION')):
                violations.append({'sample_id': row['sample_id'], 'event': kind})
        if candidate and trace.get('state'):
            plan_event = next(e for e in trace['events'] if e['event_type'] == 'PLAN_CREATED')
            state = replay_subqueries([RuntimeEvent.model_validate(e) for e in trace['events']],
                                      QueryPlan.model_validate(plan_event['payload']['plan']))
            assert state.export() == trace['state'], 'Replay mismatch: ' + row['sample_id']
            replayed += 1
        if row.get('error'):
            errors.append({'sample_id': row['sample_id'], 'error': row['error']})
    totals = Counter()
    for counts in interfaces.values():
        totals.update(counts)
    by_type = {}
    for kind in sorted({r['question_type'] for r in rows}):
        subset = [r for r in rows if r['question_type'] == kind]
        by_type[kind] = {'count': len(subset), 'correct': sum(r['answer_exact'] for r in subset),
                         'f1': sum(r['answer_token_f1'] for r in subset) / len(subset)}
    metrics = {'correct': sum(r['answer_exact'] for r in rows),
               'accuracy': sum(r['answer_exact'] for r in rows) / 128,
               'f1': sum(r['answer_token_f1'] for r in rows) / 128,
               'statuses': dict(Counter(r['status'] for r in rows)),
               'runtime_statuses': dict(Counter(r.get('runtime_status') for r in rows)),
               'cost': dict(totals), 'interfaces': {k: dict(v) for k, v in interfaces.items()},
               'latency_p50_ms': percentile([r['latency_ms'] for r in rows], .5),
               'latency_p95_ms': percentile([r['latency_ms'] for r in rows], .95),
               'by_type': by_type, 'events': dict(events), 'errors': errors,
               'update_protocol': dict(update_protocol), 'replayed_states': replayed,
               'violations': violations}
    return {r['sample_id']: r for r in rows}, metrics, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'results/ultimate/v04_joint_source_memory_full128_retry3')
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    before, parent, pm = audit_run(args.baseline, candidate=False)
    after, candidate, cm = audit_run(args.candidate, candidate=True)
    for key in ('sample_ids', 'data_sha256', 'tokenizer_sha256'):
        assert pm[key] == cm[key], key
    configs = [read_json(d / 'config.resolved.json') for d in (args.baseline, args.candidate)]
    for config in configs:
        for key in ('experiment_id', 'output_dir', 'config_fingerprint'):
            config.pop(key, None)
    assert configs[0] == configs[1], 'Config drift beyond run identity'
    paired = []
    for sid in cm['sample_ids']:
        p, c = before[sid], after[sid]
        assert all(p[k] == c[k] for k in ('question', 'question_type', 'gold_answers'))
        flip = 'STABLE' if p['answer_exact'] == c['answer_exact'] else 'GAIN' if c['answer_exact'] else 'LOSS'
        paired.append({'sample_id': sid, 'question_type': c['question_type'], 'flip': flip,
                       'question': c['question'], 'gold': json.dumps(c['gold_answers'], ensure_ascii=False),
                       'parent_prediction': p['prediction'], 'candidate_prediction': c['prediction'],
                       'parent_exact': p['answer_exact'], 'candidate_exact': c['answer_exact'],
                       'parent_f1': p['answer_token_f1'], 'candidate_f1': c['answer_token_f1'],
                       'parent_status': p['status'], 'candidate_status': c['status']})
    checks = {
        'accuracy_vs_v04': candidate['correct'] >= parent['correct'],
        'f1_vs_v04': candidate['f1'] >= parent['f1'] - 1e-12,
        'comparison_35_protected': candidate['by_type']['comparison']['correct'] == 35,
        'no_more_errors': candidate['statuses'].get('ERROR', 0) <= parent['statuses'].get('ERROR', 0),
        'no_more_resource_limits': candidate['runtime_statuses'].get('RESOURCE_LIMIT', 0) <= parent['runtime_statuses'].get('RESOURCE_LIMIT', 0),
        'runtime_invariants': not candidate['violations'],
    }
    failures = [name for name, passed in checks.items() if not passed]
    reason = ('Single-run checks failed: ' + ', '.join(failures) + '. ' if failures else '')
    reason += 'One completed run; strict three-run paired acceptance has not been performed.'
    result = {'status': 'BLOCKED', 'single_run_checks_passed': all(checks.values()),
              'reason': reason,
              'baseline_run': str(args.baseline), 'candidate_run': str(args.candidate),
              'sample_count': 128, 'parent': parent, 'candidate': candidate, 'checks': checks,
              'gains': [r['sample_id'] for r in paired if r['flip'] == 'GAIN'],
              'losses': [r['sample_id'] for r in paired if r['flip'] == 'LOSS'],
              'candidate_source_sha256': cm['source_sha256']}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / 'gate.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    with (args.report_dir / 'paired_diff.csv').open('w') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(paired[0]))
        writer.writeheader()
        writer.writerows(paired)
    print(json.dumps({k: result[k] for k in ('status', 'checks', 'gains', 'losses')}, indent=2))
    print(json.dumps({k: candidate[k] for k in ('correct', 'accuracy', 'f1', 'statuses', 'cost', 'update_protocol', 'replayed_states')}, indent=2))


if __name__ == '__main__':
    main()
