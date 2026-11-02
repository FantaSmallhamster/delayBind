"""Prepare all MuSiQue-Ans dev questions for blind inference; keep gold separate."""
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.data import load_records


def main():
    source = ROOT / 'data/raw/musique_v1.0/musique_ans_v1.0_dev.jsonl'
    dest = ROOT / 'data/test/musique_ans_v1.0_dev'
    if dest.exists():
        raise SystemExit('Prepared output already exists; never overwrite it silently')
    data = [json.loads(line) for line in source.open() if line.strip()]
    assert len(data) == len({r['id'] for r in data}) == 2417
    inference, gold = [], []
    for row in data:
        assert row['answerable'] is True
        paragraphs = row['paragraphs']
        ids = {p['idx'] for p in paragraphs}
        assert len(ids) == len(paragraphs)
        pieces, support, mapping, offset = [], [], [], 0
        for display_index, paragraph in enumerate(paragraphs, 1):
            text = paragraph['title'] + '\n' + paragraph['paragraph_text']
            assert not re.search(r'(?m)^Document\s+\d+:\s*\n', text), 'Ambiguous document header in source'
            piece = f'Document {display_index}:\n' + text + '\n\n'
            content_start = offset + len(f'Document {display_index}:\n')
            mapping.append({'display_document_number': display_index, 'official_paragraph_idx': paragraph['idx'],
                            'title': paragraph['title'], 'context_content_start': content_start,
                            'context_content_end': content_start + len(text)})
            if paragraph['is_supporting']:
                support.append(paragraph['idx'])
            pieces.append(piece)
            offset += len(piece)
        context = ''.join(pieces)
        for p, m in zip(paragraphs, mapping):
            assert context[m['context_content_start']:m['context_content_end']] == p['title'] + '\n' + p['paragraph_text']
        inference.append({'id': row['id'], 'input': row['question'], 'context': context,
                          'num_docs': len(paragraphs), 'type': row['id'].split('__')[0]})
        gold.append({'sample_id': row['id'], 'official_id': row['id'], 'question': row['question'],
                     'type': row['id'].split('__')[0],
                     'gold_answers': list(dict.fromkeys([row['answer'], *row['answer_aliases']])),
                     'answer': row['answer'], 'answer_aliases': row['answer_aliases'], 'answerable': True,
                     'supporting_paragraph_ids': support, 'paragraph_mapping': mapping,
                     'question_decomposition': row['question_decomposition'],
                     'support_annotation_level': 'paragraph; not sentence'})
    dest.mkdir(parents=True)
    inp = dest / 'inference.json'
    ann = dest / 'gold_answers.jsonl'
    inp.write_text(json.dumps(inference, ensure_ascii=False) + '\n')
    ann.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in gold))
    loaded = list(load_records(inp, dataset_id='musique'))
    assert len(loaded) == 2417
    assert all(s.context and not s.answer and not s.answers and not s.supporting_facts and not s.evidences for s in loaded)
    assert [s.sample_id for s in loaded] == [r['id'] for r in data]
    manifest = {'source': str(source.relative_to(ROOT)), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'count': len(inference), 'original_split': 'MuSiQue-Ans v1.0 dev', 'order_preserved': True,
                'all_original_paragraphs_preserved': True, 'annotations_excluded_from_inference': True,
                'loader_smoke_check': '2417 records loaded; no answers/support/decomposition in inference inputs',
                'model_calls': 0,
                'files': {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in (inp, ann)}}
    (dest / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    (dest / 'README.md').write_text('''# MuSiQue-Ans v1.0 开发集（框架输入）

包含官方 dev 划分的全部 2,417 题，未抽样、未重排，未调用模型。

- `inference.json`：当前框架可直接读取。保留原问题、原段落顺序、全部标题与正文；只添加 Document 展示边界。
- `gold_answers.jsonl`：独立评分文件，含答案、别名、支持段落编号、原问题分解，以及原段落编号与展示位置映射。
- `manifest.json`：来源哈希、输出哈希和读取检查结果。

每题有 17–20 个段落，不能把 `expected_documents` 固定为 20。原始 idx 不保证连续，评分映射使用原 idx。
官方标注是段落级，本转换没有伪造句子级支持标注。原问题分解和答案均不进入普通推理输入。
`load_records(..., dataset_id="musique")` 已完成全量无模型读取检查；尚未启动模型评测。

官方 test 文件没有公开答案，不能直接用于本地准确率评分。此目录始终标为 dev，不能改称独立官方 test。
MuSiQue-Full 的相同问题 ID 有两个实例，本目录不转换 Full，也不合并或删除其原始配对样本。

原始镜像来源、固定版本和校验信息见 `../../raw/musique_v1.0/download_manifest.json`。
''', encoding='utf-8')
    print(json.dumps({'prepared': str(dest), 'count': len(loaded), 'model_calls': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
