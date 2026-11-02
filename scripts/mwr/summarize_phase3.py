"""Recompute the completed phase-three diagnostic totals without model calls."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'reports/mwr/phase3_20261029'


def read(name):
    return json.loads((REPORT / name).read_text())


def main():
    synthetic = read('controlled_reading.json')
    real = read('frozen_reading_ab.json')
    before = read('controlled_pressure.json')
    after = read('controlled_pressure_postfix.json')
    assert before['rows'] == after['rows'], 'Performance patch changed retention/events'
    assert len(before['rows']) == 128
    assert all(c['live_tokens'] <= r['capacity'] for r in after['rows'] for c in r['commits'])
    rows = synthetic['rows'] + real['rows']
    assert len(rows) == 120
    usage = {key: sum(row['usage'][key] for row in rows) for key in (
        'physical_attempts', 'cache_hits', 'failed_attempts', 'usage_unknown_attempts',
        'known_input_tokens', 'known_output_tokens')}
    pairs = {}
    for row in synthetic['rows']:
        pairs.setdefault(row['case_id'], {})[row['variant']] = row
    differences = [r['unknown_explicit']['captured_expected'] - r['unknown_current']['captured_expected'] for r in pairs.values()]
    real_pairs = {}
    for row in real['rows']:
        real_pairs.setdefault(row['case_index'], {})[row['variant']] = row
    summary = {
        'date': '2026-10-29', 'scope': 'Development diagnostics and deterministic engineering controls only',
        'synthetic_reading': synthetic['groups'], 'synthetic_pairs': {
            'improved': sum(d > 0 for d in differences), 'regressed': sum(d < 0 for d in differences),
            'unchanged': sum(d == 0 for d in differences)},
        'frozen_real_windows': real['groups'],
        'real_window_support_flags_identical': all(r['current']['support_flags'] == r['explicit']['support_flags'] for r in real_pairs.values()),
        'pressure': before['groups'], 'pressure_conditions': 128,
        'pressure_conditions_with_evictions': sum(r['eviction_events'] > 0 for r in before['rows']),
        'pressure_commits_per_replay': sum(len(r['commits']) for r in before['rows']),
        'performance_patch_all_128_rows_and_events_equal': True,
        'all_fit_before': read('all_fit_before.json'), 'all_fit_after': read('all_fit_after.json'),
        'new_real_model_usage': usage, 'formal_512_new_model_calls': 0,
        'candidate_prompt_adopted': False,
        'production_change': 'All-fit retention counts the unchanged state once for all log rows',
        'excluded_preliminary_control': 'controlled_pressure_preliminary.json used the wrong stage/retention/oracle event order; archived, not included in conclusions',
        'tests': (REPORT / 'final-tests.txt').read_text().splitlines()[-1],
    }
    (REPORT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    files = [*sorted((ROOT / 'delaybind_core').rglob('*.py')),
             *sorted((ROOT / 'tests/mwr').glob('*.py')),
             *sorted((ROOT / 'scripts/mwr').glob('*.py')),
             ROOT / 'reports/mwr/PROGRESS.md',
             *[p for p in sorted(REPORT.iterdir()) if p.is_file()]]
    files = [p for p in files if p.name != 'implementation-manifest.json']
    (REPORT / 'implementation-manifest.json').write_text(json.dumps({
        'files': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    }, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'usage': usage, 'pressure_rows_equal': True, 'tests': summary['tests']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
