"""Audit one hints-visible ANSWER run against both retained baselines."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ultimate.audit_three_column_update import audit_run, read_json
from delaybind_core.update_json import update_response_format


def normalized_config(directory):
    config = read_json(directory / 'config.resolved.json')
    for key in ('experiment_id', 'output_dir', 'config_fingerprint'):
        config.pop(key, None)
    for key in ('update_protocol', 'update_json_mode'):
        config['runner'].pop(key, None)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--json-baseline', type=Path, required=True)
    parser.add_argument('--text-baseline', type=Path, required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    args = parser.parse_args()
    rows, metrics, manifest = audit_run(args.candidate, candidate=True, json_update=True)
    config = read_json(args.candidate / 'config.resolved.json')
    mode = config['runner']['update_json_mode']
    assert config['runner']['update_protocol'] == 'json'
    request_violations, visibility_violations = [], []
    counts, rejected = Counter(), Counter()
    hints_by_sample, answer_calls_by_sample = {}, {}
    for sid, row in rows.items():
        trace = read_json(args.candidate / 'trajectories' / Path(row['trajectory_path']).name)
        hints = (trace.get('state') or {}).get('hints', [])
        hints_by_sample[sid] = hints
        counts['samples_with_hints'] += bool(hints)
        counts['saved_hints'] += len(hints)
        answer_calls = [call for call in trace['model_calls'] if call['interface'] == 'ANSWER']
        answer_calls_by_sample[sid] = answer_calls
        counts['samples_with_answer_call'] += bool(answer_calls)
        for call in trace['model_calls']:
            expected = update_response_format(mode) if call['interface'] == 'UPDATE' else None
            if call['raw_request'].get('response_format') != expected:
                request_violations.append({'sample_id': sid, 'call_id': call['call_id']})
        for call in answer_calls:
            prompt = '\n'.join(m.get('content', '') for m in call['raw_request']['messages'])
            counts['answer_requests'] += 1
            if 'Source-backed hints not yet attached to a specific query:' not in prompt:
                visibility_violations.append({'sample_id': sid, 'reason': 'missing hints section'})
            if hints and json.dumps(hints, ensure_ascii=False) not in prompt:
                visibility_violations.append({'sample_id': sid, 'reason': 'saved hints missing from ANSWER'})
        for event in trace['events']:
            if event['event_type'] == 'UPDATE_JSON_REJECTED':
                rejected[event['payload']['category']] += 1
    comparisons = {}
    paired_tables = {}
    baseline_rows = {}
    for label, directory in [('json', args.json_baseline), ('three_column', args.text_baseline)]:
        old, old_metrics, old_manifest = audit_run(directory, candidate=False)
        baseline_rows[label] = old
        for key in ('sample_ids', 'data_sha256', 'tokenizer_sha256'):
            assert old_manifest[key] == manifest[key], key
        assert normalized_config(directory) == normalized_config(args.candidate), 'Config drift: ' + label
        paired = []
        for sid in manifest['sample_ids']:
            a, b = old[sid], rows[sid]
            assert all(a[k] == b[k] for k in ('question', 'question_type', 'gold_answers'))
            flip = 'STABLE' if a['answer_exact'] == b['answer_exact'] else 'GAIN' if b['answer_exact'] else 'LOSS'
            paired.append({'sample_id': sid, 'question_type': b['question_type'], 'flip': flip,
                'question': b['question'], 'gold': json.dumps(b['gold_answers'], ensure_ascii=False),
                'baseline_prediction': a['prediction'], 'candidate_prediction': b['prediction'],
                'baseline_exact': a['answer_exact'], 'candidate_exact': b['answer_exact'],
                'baseline_f1': a['answer_token_f1'], 'candidate_f1': b['answer_token_f1'],
                'baseline_status': a['status'], 'candidate_status': b['status']})
        paired_tables[label] = paired
        comparisons[label] = {'run': str(directory), 'metrics': old_metrics,
            'correct_delta': metrics['correct'] - old_metrics['correct'],
            'accuracy_delta': metrics['accuracy'] - old_metrics['accuracy'],
            'f1_delta': metrics['f1'] - old_metrics['f1'],
            'gains': [r['sample_id'] for r in paired if r['flip'] == 'GAIN'],
            'losses': [r['sample_id'] for r in paired if r['flip'] == 'LOSS']}
    assert not request_violations and not visibility_violations and not metrics['violations']
    probes = set(comparisons['json']['losses']) | {
        'dev_5547', 'dev_3890', 'dev_5351', 'dev_4298', 'dev_6901',
        'dev_7403', 'dev_12309', 'dev_4237', 'dev_4961'}
    evidence = {}
    for sid in sorted(probes):
        row = rows[sid]
        trace = read_json(args.candidate / 'trajectories' / Path(row['trajectory_path']).name)
        evidence[sid] = {'question': row['question'], 'gold': row['gold_answers'],
            'prediction': row['prediction'], 'exact': row['answer_exact'],
            'previous_json_prediction': baseline_rows['json'][sid]['prediction'],
            'previous_json_exact': baseline_rows['json'][sid]['answer_exact'],
            'hints': hints_by_sample[sid], 'state': trace.get('state'),
            'answer_calls': answer_calls_by_sample[sid],
            'update_calls': [c for c in trace['model_calls'] if c['interface'] == 'UPDATE']}
    result = {'experiment': 'V06a query-uncertain facts in hints and hints visible to ANSWER',
        'status': 'ONE_RUN_COMPLETE', 'candidate_run': str(args.candidate), 'sample_count': 128,
        'candidate': metrics, 'comparisons': comparisons,
        'hints_visibility': dict(counts), 'visibility_violations': visibility_violations,
        'request_format_violations': request_violations, 'json_rejection_categories': dict(rejected),
        'source_sha256': manifest['source_sha256'],
        'strict_stage_acceptance': 'Not established: only one run was requested; three fresh paired runs have not been performed.',
        'limitations': ['Hints include extracted text and refs, but hint refs are not additionally added to final original-source reread.',
            'The existing working-memory-only wording remains alongside the explicit supplementary-hints permission.',
            'Both UPDATE guidance and ANSWER visibility changed; paired flips alone do not isolate their individual effects.']}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    (args.report_dir / 'case_evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    for label, table in paired_tables.items():
        with (args.report_dir / ('paired_vs_' + label + '.csv')).open('w') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    print(json.dumps({'candidate': {k: metrics[k] for k in ('correct', 'accuracy', 'f1', 'statuses', 'by_type', 'cost')},
        'comparisons': {k: {n: v[n] for n in ('correct_delta', 'f1_delta', 'gains', 'losses')} for k, v in comparisons.items()},
        'hints_visibility': dict(counts)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
