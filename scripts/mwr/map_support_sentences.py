"""Offline alignment of official sentence arrays to immutable inference text."""
import argparse
import bisect
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mwr.bundle_tools import documents, sha
from delaybind_core.mwr.common import digest


def align_sentences(title, sentences, body):
    """Align the whole document, ignoring only whitespace; never fuzzy match.

    Offsets are Python Unicode character offsets in the original body, including
    its title. Trimming sentence-edge whitespace avoids ambiguous boundaries.
    Position-based alignment also disambiguates repeated identical sentences.
    """
    if not isinstance(sentences, list) or not all(isinstance(s, str) for s in sentences):
        raise ValueError('official context must contain sentence arrays')
    canonical = title + '\n' + ''.join(sentences)
    official_positions = [i for i, c in enumerate(canonical) if not c.isspace()]
    raw_positions = [i for i, c in enumerate(body) if not c.isspace()]
    equal = ''.join(canonical[i] for i in official_positions) == ''.join(body[i] for i in raw_positions)
    status = ('EXACT' if canonical == body.rstrip('\n') else 'WHITESPACE_ALIGNED') if equal else 'UNCERTAIN'
    result = []
    offset = len(title) + 1
    for index, sentence in enumerate(sentences):
        row = {'official_sentence_index': index, 'official_sentence_text': sentence, 'mapping_status': status}
        left = bisect.bisect_left(official_positions, offset)
        right = bisect.bisect_left(official_positions, offset + len(sentence))
        if not equal:
            row['reason'] = 'whole document differs in non-whitespace characters'
        elif left == right:
            row.update(mapping_status='UNCERTAIN', reason='empty official sentence')
        else:
            start, end = raw_positions[left], raw_positions[right - 1] + 1
            row.update(start=start, end=end, text=body[start:end],
                       sentence_text_equal=body[start:end] == sentence.strip())
        result.append(row)
        offset += len(sentence)
    return result


def map_bundle(bundle, official_path, document_maps, source_path=None):
    bundle, official_path = Path(bundle), Path(official_path)
    data = json.loads((bundle / 'test/eval_2wikimultihopqa_50.json').read_text())
    gold = [json.loads(s) for s in (bundle / 'grading/gold_answers.jsonl').read_text().splitlines() if s.strip()]
    official = json.loads(official_path.read_text())
    by_gold = {r['sample_id']: r for r in gold}
    by_official = {r['_id']: r for r in official}
    by_map = {r['sample_id']: r for r in document_maps}
    ids = {r['id'] for r in data}
    if len(ids) != len(data) or len(by_gold) != len(gold) or len(by_map) != len(document_maps):
        raise ValueError('duplicate sample IDs')
    if ids != set(by_gold) or ids != set(by_map):
        raise ValueError('input/gold/document map IDs differ')
    if len(by_official) != len(official) or set(by_official) != {g['official_id'] for g in gold}:
        raise ValueError('official IDs differ or repeat')
    report = {'official_path': str(official_path.resolve()), 'official_sha256': sha(official_path.read_bytes()),
              'test_sha256': sha((bundle / 'test/eval_2wikimultihopqa_50.json').read_bytes()),
              'gold_sha256': sha((bundle / 'grading/gold_answers.jsonl').read_bytes()),
              'sample_count': len(data), 'question_text_exact': 0, 'question_edge_whitespace_only': 0,
              'identical_official_document_duplicates': 0,
              't_star_status': 'PENDING_INDEPENDENT_QUERY_BRIDGE_ANNOTATIONS',
              'offset_contract': 'zero-based Unicode character [start,end) within original document body including title; sentence edge whitespace excluded'}
    if source_path is not None:
        source_path = Path(source_path)
        expected = json.loads((bundle / 'metadata/selection_summary.json').read_text())['input_sha256']
        if sha(source_path.read_bytes()) != expected:
            raise ValueError('full source hash differs from bundle metadata')
        source = json.loads(source_path.read_text())
        by_source = {r['id']: r for r in source}
        if len(by_source) != len(source) or set(by_source) != ids:
            raise ValueError('full source sample IDs differ')
        for row in data:
            src, g = by_source[row['id']], by_gold[row['id']]
            if any(src[k] != row[k] for k in ('context', 'input', 'type', 'num_docs')):
                raise ValueError('full source inference text differs')
            if src['answers'] != g['gold_answers'] or src['official_id'] != g['official_id']:
                raise ValueError('full source gold differs')
        report['full_source_verification'] = {'path': str(source_path.resolve()), 'sha256': expected,
                                              'all_inference_text_and_gold_equal': True}
    facts = []
    for row in data:
        g, mapping = by_gold[row['id']], by_map[row['id']]
        o = by_official[g['official_id']]
        if mapping['official_id'] != g['official_id'] or o['supporting_facts'] != g['supporting_facts']:
            raise ValueError('official supporting facts differ')
        if o['question'] == row['input']:
            report['question_text_exact'] += 1
        elif o['question'].strip() == row['input'].strip():
            report['question_edge_whitespace_only'] += 1
        else:
            raise ValueError('official question differs beyond edge whitespace')
        contexts = {}
        for title, sentences in o['context']:
            if title in contexts:
                if contexts[title] != sentences:
                    raise ValueError('ambiguous repeated official document title')
                report['identical_official_document_duplicates'] += 1
            contexts[title] = sentences
        docs = documents(row['context'])
        mapped_docs = {d['title']: d for d in mapping['support_documents']}
        if len(mapped_docs) != len(mapping['support_documents']):
            raise ValueError('ambiguous repeated support document title')
        aligned = {}
        for title, mapped in mapped_docs.items():
            doc = docs[mapped['index']]
            if (doc['title'] != title or mapped['doc_id'] != 'd-' + digest(['', doc['text']])[:24]
                    or mapped['source_hash'] != digest(doc['text'])
                    or mapped['context_start'] != doc['context_start'] or mapped['context_end'] != doc['context_end']):
                raise ValueError('stale document map')
            if title not in contexts:
                raise ValueError('support document absent in official context')
            aligned[title] = align_sentences(title, contexts[title], doc['text'])
        selected = {title: [] for title in mapped_docs}
        for fact_index, (title, sentence_index) in enumerate(g['supporting_facts']):
            if title not in aligned or type(sentence_index) is not int or not 0 <= sentence_index < len(aligned[title]):
                raise ValueError('official support sentence index out of range')
            doc = mapped_docs[title]
            sentence = aligned[title][sentence_index]
            fact = {'sample_id': row['id'], 'original_question_id': g['official_id'],
                    'official_id': g['official_id'], 'support_fact_index': fact_index, 'title': title,
                    'document_index': doc['index'], 'doc_id': doc['doc_id'], 'source_hash': doc['source_hash'], **sentence}
            if 'start' in sentence:
                fact.update(context_start=doc['context_start'] + sentence['start'],
                            context_end=doc['context_start'] + sentence['end'])
                if row['context'][fact['context_start']:fact['context_end']] != fact['text']:
                    raise ValueError('raw context slice mismatch')
            facts.append(fact)
            selected[title].append(sentence)
        for title, doc in mapped_docs.items():
            statuses = {s['mapping_status'] for s in selected[title]}
            doc['sentence_mapping_status'] = ('UNCERTAIN' if 'UNCERTAIN' in statuses else
                                               'WHITESPACE_ALIGNED' if 'WHITESPACE_ALIGNED' in statuses else 'EXACT')
            doc['support_sentences'] = selected[title]
    report['support_fact_count'] = len(facts)
    report['support_fact_mapping_counts'] = dict(Counter(f['mapping_status'] for f in facts))
    report['support_document_mapping_counts'] = dict(Counter(d['sentence_mapping_status'] for m in document_maps for d in m['support_documents']))
    report['all_support_fact_raw_offsets_verified'] = all('start' in f for f in facts)
    return report, facts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--official', type=Path, required=True)
    p.add_argument('--full-source', type=Path)
    p.add_argument('--report-dir', type=Path, required=True)
    a = p.parse_args()
    map_path = a.report_dir / 'support_document_map.jsonl'
    maps = [json.loads(s) for s in map_path.read_text().splitlines() if s.strip()]
    report, facts = map_bundle(a.bundle, a.official, maps, a.full_source)
    (a.report_dir / 'sentence_mapping_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (a.report_dir / 'support_sentence_map.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in facts))
    map_path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in maps))
    audit_path = a.report_dir / 'audit.json'
    audit = json.loads(audit_path.read_text())
    audit['sentence_mapping'] = report
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
