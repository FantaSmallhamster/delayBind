"""Pair V06a with the frozen three-column parent and audit JSON request scope."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ultimate.audit_three_column_update import audit_run, read_json
from delaybind_core.fact_protocol import parse_update
from delaybind_core.update_json import parse_update_json, update_response_format


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'results/ultimate/v04_three_column_update_full128_r1')
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    old_rows, parent, pm = audit_run(args.baseline, candidate=False)
    new_rows, candidate, cm = audit_run(args.candidate, candidate=True, json_update=True)
    for key in ('sample_ids', 'data_sha256', 'tokenizer_sha256'):
        assert pm[key] == cm[key], key
    old, new = [read_json(d / 'config.resolved.json') for d in (args.baseline, args.candidate)]
    assert new['runner']['update_protocol'] == 'json'
    mode = new['runner']['update_json_mode']
    for cfg in (old, new):
        for key in ('experiment_id', 'output_dir', 'config_fingerprint'):
            cfg.pop(key, None)
        cfg['runner'].pop('update_protocol', None)
        cfg['runner'].pop('update_json_mode', None)
    assert old == new, 'Unexpected config drift'
    paired, losses, gains = [], [], []
    diagnostics = {}
    for label, directory, rows in [('parent', args.baseline, old_rows), ('candidate', args.candidate, new_rows)]:
        reasons, categories = Counter(), Counter()
        adapter_shapes, observed_events, coverage = Counter(), Counter(), Counter()
        repaired_samples, exhausted, rejected_samples, transport_errors = set(), set(), set(), []
        update_attempts = 0
        for sid, row in rows.items():
            trace = read_json(directory / 'trajectories' / Path(row['trajectory_path']).name)
            for call in trace['model_calls']:
                if call['interface'] == 'UPDATE':
                    update_attempts += 1
                    raw = call.get('raw_response') or {}
                    choice = (raw.get('choices') or [{}])[0]
                    reasons[str(choice.get('finish_reason', 'NO_RESPONSE'))] += 1
                    if call.get('parsed_output') is not None and not call.get('error'):
                        adapter_shapes['returned_responses'] += 1
                        try:
                            parsed = (parse_update_json(call['parsed_output']) if label == 'candidate'
                                      else parse_update(call['parsed_output'], recover=True))
                            adapter_shapes['parsed_responses'] += 1
                            adapter_shapes['responses_with_bad_items'] += bool(parsed.rejected_lines)
                            adapter_shapes['fact_items'] += len(parsed.facts)
                            adapter_shapes['hint_items'] += len(parsed.hints)
                            adapter_shapes['ignored_lines'] += len(parsed.ignored_lines)
                        except ValueError:
                            adapter_shapes['unparseable_responses'] += 1
                    if label == 'candidate' and call['raw_request'].get('response_format') != update_response_format(mode):
                        transport_errors.append({'sample_id': sid, 'call_id': call['call_id']})
                elif label == 'candidate' and call['raw_request'].get('response_format') is not None:
                    transport_errors.append({'sample_id': sid, 'interface': call['interface']})
            for event in trace['events']:
                kind, payload = event['event_type'], event['payload']
                observed_events[kind] += 1
                if kind == 'UPDATE_REPAIR_REQUESTED':
                    repaired_samples.add(sid)
                if kind == 'UPDATE_JSON_REJECTED':
                    categories[payload['category']] += 1
                    rejected_samples.add(sid)
                if kind == 'PROTOCOL_REPAIR_EXHAUSTED' and payload.get('interface') == 'UPDATE':
                    exhausted.add(sid)
            state = trace.get('state') or {}
            coverage['stored_fact_objects'] += len(state.get('facts', {}))
            coverage['query_fact_uses'] += sum(len(uses) for uses in state.get('uses', {}).values())
            coverage['queries_with_facts'] += sum(bool(uses) for uses in state.get('uses', {}).values())
            coverage['resolved_queries'] += sum(e['status'] == 'RESOLVED' for e in state.get('executions', {}).values())
        diagnostics[label] = {'update_network_and_cache_records': update_attempts,
            'finish_reasons': dict(reasons), 'json_rejection_categories': dict(categories),
            'repaired_samples': sorted(repaired_samples), 'exhausted_samples': sorted(exhausted),
            'rejected_samples': sorted(rejected_samples), 'transport_violations': transport_errors,
            'adapter_shape_counts_without_source_id_validation': dict(adapter_shapes),
            'actual_local_validation_events': {k: observed_events[k] for k in
                ('UPDATE_LINE_REJECTED', 'UPDATE_LINES_DROPPED', 'UPDATE_REPAIR_REQUESTED',
                 'UPDATE_JSON_PARSED', 'UPDATE_JSON_REJECTED')},
            'coverage_proxies_not_gold_recall': dict(coverage)}
    for sid in cm['sample_ids']:
        a, b = old_rows[sid], new_rows[sid]
        assert all(a[k] == b[k] for k in ('question', 'question_type', 'gold_answers'))
        flip = 'STABLE' if a['answer_exact'] == b['answer_exact'] else 'GAIN' if b['answer_exact'] else 'LOSS'
        if flip == 'GAIN': gains.append(sid)
        if flip == 'LOSS': losses.append(sid)
        paired.append({'sample_id': sid, 'question_type': b['question_type'], 'flip': flip,
            'question': b['question'], 'gold': json.dumps(b['gold_answers'], ensure_ascii=False),
            'parent_prediction': a['prediction'], 'candidate_prediction': b['prediction'],
            'parent_exact': a['answer_exact'], 'candidate_exact': b['answer_exact'],
            'parent_f1': a['answer_token_f1'], 'candidate_f1': b['answer_token_f1'],
            'parent_status': a['status'], 'candidate_status': b['status']})
    checks = {'accuracy_vs_parent': candidate['correct'] >= parent['correct'],
        'f1_vs_parent': candidate['f1'] >= parent['f1'] - 1e-12,
        'historical_c_floor': candidate['correct'] >= 92 and candidate['f1'] >= 0.735453869047619 - 1e-12,
        'comparison_35_protected': candidate['by_type']['comparison']['correct'] == 35,
        'no_more_errors': candidate['statuses'].get('ERROR', 0) <= parent['statuses'].get('ERROR', 0),
        'no_more_resource_limits': candidate['runtime_statuses'].get('RESOURCE_LIMIT', 0) <= parent['runtime_statuses'].get('RESOURCE_LIMIT', 0),
        'runtime_and_interface_invariants': not candidate['violations'] and not diagnostics['candidate']['transport_violations']}
    result = {'stage': 'V06a', 'status': 'BLOCKED', 'baseline_run': str(args.baseline),
        'candidate_run': str(args.candidate), 'sample_count': 128,
        'single_run_checks_passed': all(checks.values()), 'checks': checks,
        'reason': 'Strict promotion needs all quality gates and the predeclared three fresh paired runs; this report covers one run.',
        'parent': parent, 'candidate': candidate, 'protocol_diagnostics': diagnostics,
        'gains': gains, 'losses': losses, 'source_sha256': cm['source_sha256'],
        'transport_mode': mode, 'unit_tests': {'status': 'PASS', 'count': 100}}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / 'gate.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    with (args.report_dir / 'paired_diff.csv').open('w') as f:
        w = csv.DictWriter(f, fieldnames=list(paired[0])); w.writeheader(); w.writerows(paired)
    print(json.dumps({k: result[k] for k in ('status', 'checks', 'gains', 'losses')}, indent=2))
    print(json.dumps({k: candidate[k] for k in ('correct', 'accuracy', 'f1', 'statuses', 'cost', 'update_protocol', 'replayed_states')}, indent=2))
    print(json.dumps(diagnostics['candidate'], indent=2))


if __name__ == '__main__':
    main()
