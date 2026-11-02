"""Stream-validate downloaded MuSiQue splits and record their actual inventories."""
from collections import Counter
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'data/raw/musique_v1.0'


def semantic_hash(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    manifest = json.loads((DATA / 'download_manifest.json').read_text())
    assert not manifest['errors'] and len(manifest['files']) == 7
    report = {'files': [], 'split_id_overlaps': {}, 'full_answerable_matches_ans': {}, 'model_calls': 0}
    sets, ans_hashes = {}, {}
    for variant in ('ans', 'full'):
        for split in ('train', 'dev', 'test'):
            path = DATA / f'musique_{variant}_v1.0_{split}.jsonl'
            ids, counts, paragraphs, hops, support_counts = Counter(), Counter(), Counter(), Counter(), Counter()
            matches = 0
            if variant == 'ans':
                ans_hashes[split] = {}
            with path.open() as handle:
                for number, line in enumerate(handle, 1):
                    row = json.loads(line)
                    assert row['id'] and isinstance(row['question'], str) and row['question'].strip()
                    para = row['paragraphs']
                    assert para and all(isinstance(p['title'], str) and isinstance(p['paragraph_text'], str) for p in para)
                    para_ids = {p['idx'] for p in para}
                    assert len(para_ids) == len(para)
                    ids[row['id']] += 1
                    counts['records'] += 1
                    paragraphs[len(para)] += 1
                    hops[row['id'].split('__')[0]] += 1
                    counts['with_answer'] += bool(row.get('answer'))
                    counts['with_decomposition'] += bool(row.get('question_decomposition'))
                    counts['answerable'] += row.get('answerable') is True
                    counts['unanswerable'] += row.get('answerable') is False
                    if 'answerable' in row:
                        assert isinstance(row['answerable'], bool)
                        assert all(isinstance(p['is_supporting'], bool) for p in para)
                        support_counts[sum(p['is_supporting'] for p in para)] += 1
                    if variant == 'ans':
                        ans_hashes[split][row['id']] = semantic_hash(row)
                    elif row.get('answerable') is True or split == 'test':
                        matches += semantic_hash(row) == ans_hashes[split].get(row['id'])
            assert set(ids.values()) == ({1} if variant == 'ans' else {2})
            if split == 'test':
                assert counts['with_answer'] == counts['with_decomposition'] == 0
            else:
                assert counts['with_answer'] == counts['records']
                assert counts['answerable'] == len(ids)
                if variant == 'full':
                    assert counts['unanswerable'] == len(ids)
            sets[variant, split] = set(ids)
            if variant == 'full':
                report['full_answerable_matches_ans'][split] = {'matching_records': matches, 'ans_records': len(ans_hashes[split]),
                    'note': 'Test has no answerability label; counts byte-equivalent JSON objects among both instances.'}
            report['files'].append({'file': path.name, 'bytes': path.stat().st_size, **counts,
                                   'unique_question_ids': len(ids), 'instances_per_id': dict(Counter(ids.values())),
                                   'paragraph_count_distribution': dict(sorted(paragraphs.items())),
                                   'hop_distribution': dict(sorted(hops.items())), 'support_paragraph_count_distribution': dict(sorted(support_counts.items()))})
    for variant in ('ans', 'full'):
        for a, b in (('train', 'dev'), ('train', 'test'), ('dev', 'test')):
            overlap = sets[variant, a] & sets[variant, b]
            report['split_id_overlaps'][f'{variant}:{a}/{b}'] = len(overlap)
            assert not overlap
    (DATA / 'inventory.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'files': [{k: r[k] for k in ('file', 'records', 'unique_question_ids', 'with_answer')} for r in report['files']],
                      'split_id_overlaps': report['split_id_overlaps'], 'full_answerable_matches_ans': report['full_answerable_matches_ans']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
