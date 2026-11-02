"""Offline support visibility under a frozen reading order; never infer t*."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.cli import _load_tokenizer
from delaybind_core.data import load_records
from delaybind_core.mwr.models import InferenceInput
from delaybind_core.mwr.source_access import SourceAccess


def ranges_cover(ranges, start, end):
    cursor = start
    for left, right in sorted(ranges):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def visibility(sample, facts, tokenizer, chunk_size):
    access = SourceAccess(InferenceInput.from_sample(sample), tokenizer, neighbor_anchors=0)
    coverage = defaultdict(list)
    result = []
    for fact in facts:
        if fact.get('mapping_status') != 'EXACT':
            raise ValueError('window annotations require exact raw support offsets')
        text = access._documents.get(fact['doc_id'])
        if text is None or text[fact['start']:fact['end']] != fact['text']:
            raise ValueError('support map does not match inference source')
        result.append({**fact, 'first_complete_window': None})
    while not access.exhausted:
        access.next_window(chunk_size)
        for span in access._window_refs.values():
            coverage[span.doc_id].append((span.start, span.end))
        for fact in result:
            if fact['first_complete_window'] is None and ranges_cover(coverage[fact['doc_id']], fact['start'], fact['end']):
                fact['first_complete_window'] = access.windows
    if any(f['first_complete_window'] is None for f in result):
        raise ValueError('support sentence never became completely visible')
    return {'sample_id': sample.sample_id, 'window_count': access.windows, 'support_sentences': result,
            'support_window_count': len({f['first_complete_window'] for f in result}),
            't_star_status': 'NOT_INFERRED_FROM_SOURCE_TIMING_OR_METHOD_BINDINGS'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--support-map', type=Path, required=True)
    p.add_argument('--tokenizer', type=Path, required=True)
    p.add_argument('--chunk-size', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if a.chunk_size <= 0:
        p.error('chunk-size must be positive')
    facts = [json.loads(s) for s in a.support_map.read_text().splitlines() if s.strip()]
    by_id = defaultdict(list)
    for fact in facts:
        by_id[fact['sample_id']].append(fact)
    tokenizer = _load_tokenizer(str(a.tokenizer))
    samples = list(load_records(a.input))
    if set(by_id) != {s.sample_id for s in samples}:
        raise ValueError('input/support sample IDs differ')
    rows = [visibility(s, by_id[s.sample_id], tokenizer, a.chunk_size) for s in samples]
    report = {'input_sha256': hashlib.sha256(a.input.read_bytes()).hexdigest(),
        'support_map_sha256': hashlib.sha256(a.support_map.read_bytes()).hexdigest(),
        'tokenizer_sha256': hashlib.sha256(a.tokenizer.read_bytes()).hexdigest(),
        'chunk_size': a.chunk_size, 'order': 'original', 'sample_count': len(rows),
        'window_count_distribution': dict(Counter(r['window_count'] for r in rows)),
        'samples_with_support_in_multiple_windows': sum(r['support_window_count'] > 1 for r in rows),
        'scope': 'Offline source visibility only. Multiple support windows do not prove delayed relevance; independent query/bridge annotation still required.',
        'rows': rows}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
