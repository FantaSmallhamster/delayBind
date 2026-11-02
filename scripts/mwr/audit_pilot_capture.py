"""Audit source-span capture in frozen pilot traces; not semantic support scoring."""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def covered(target, spans):
    cursor = target['start']
    for span in sorted((s for s in spans if s['doc_id'] == target['doc_id']), key=lambda s: s['start']):
        if span['start'] > cursor:
            break
        cursor = max(cursor, span['end'])
    return cursor >= target['end']


def main():
    output = Path(sys.argv[1])
    annotations = json.loads((ROOT / 'reports/mwr/phase2_20261028/dev16_source_windows.json').read_text())
    annotations = {r['sample_id']: r for r in annotations}
    rows = []
    groups = []
    for capacity in (2048, 4096, 8192):
        directory = ROOT / f'results/mwr/pilot_{capacity // 1024}k_20261028_mwr4'
        for method in ('delaybind_mwr', 'flat_memory_mwr'):
            conditions = []
            for path in sorted((directory / 'trajectories').glob(f'*-{method}-*.json')):
                trace = json.loads(path.read_text())
                proposed = [e['payload']['evidence'] for e in trace['events'] if e['event_type'] == 'EVIDENCE_PROPOSED']
                commits = [e['payload'] for e in trace['events'] if e['event_type'] == 'WINDOW_COMMITTED']
                evidence = {e['evidence_id']: e for e in proposed}
                final = commits[-1] if commits else {'evidence_ids': [], 'reachable_spans': []}
                supports = []
                for support in annotations[trace['sample_id']]['support_sentences']:
                    initial_window = support['first_complete_window']
                    observed = [e for e in proposed if e['observed_window'] <= initial_window]
                    supports.append({
                        'title': support['title'], 'sentence_index': support['official_sentence_index'],
                        'first_complete_window': initial_window,
                        'captured_on_first_complete_window': covered(support, [s for e in observed for s in e['source_spans']]),
                        'captured_at_any_window': covered(support, [s for e in proposed for s in e['source_spans']]),
                        'direct_source_retained_at_final_commit': covered(support, [s for eid in final['evidence_ids'] for s in evidence[eid]['source_spans']]),
                        'source_reachable_at_final_commit': covered(support, final['reachable_spans']),
                    })
                item = {'capacity': capacity, 'method': method, 'sample_id': trace['sample_id'],
                        'proposed': len(proposed), 'windows': len(commits),
                        'evictions': sum(e['event_type'] == 'EVIDENCE_EVICTED' for e in trace['events']),
                        'supports': supports}
                rows.append(item)
                conditions.append(item)
            supports = [s for r in conditions for s in r['supports']]
            groups.append({'capacity': capacity, 'method': method, 'conditions': len(conditions),
                           'proposed_total': sum(r['proposed'] for r in conditions),
                           'proposal_count_distribution': dict(sorted(Counter(r['proposed'] for r in conditions).items())),
                           'evictions': sum(r['evictions'] for r in conditions),
                           'support_sentence_denominator': len(supports),
                           **{key: sum(s[key] for s in supports) for key in (
                               'captured_on_first_complete_window', 'captured_at_any_window',
                               'direct_source_retained_at_final_commit', 'source_reachable_at_final_commit')}})
    report = {'scope': 'Frozen 16-question development pilot, source interval coverage only. '
              'Not cue faithfulness, semantic relevance, t-star survival, or formal test results. '
              'Previously flagged ambiguous/unsupported gold annotations remain in this positional audit.',
              'groups': groups, 'rows': rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
