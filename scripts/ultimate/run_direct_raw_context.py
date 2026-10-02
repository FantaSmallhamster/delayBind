"""One plain full-context ANSWER request per question, without chunking."""
import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core import cli
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.metrics import answer_exact, answer_token_f1
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import parse_final_answer

TEMPLATE = ("Answer the question using the context below. "
            "Give only the short final answer in \\boxed{}, without explanation.\n\n"
            "Context:\n{context}\n\nQuestion:\n{question}\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prompt(row):
    return TEMPLATE.replace('{context}', row['context']).replace('{question}', row['input'])


async def run(config, rows, output):
    if not os.environ.get('MODEL_API_KEY'):
        os.environ['MODEL_API_KEY'] = getpass.getpass('API key (hidden, memory only): ')
    api = cli._api_config(config, default_seed=4)
    semaphore = asyncio.Semaphore(config['max_concurrency'])
    completed = 0

    async def one(index, row):
        nonlocal completed
        async with semaphore:
            run_id = f"{config['experiment_id']}-{row['id']}"
            store = SQLiteEventStore(str(output / 'model_calls.sqlite'))
            client = OpenAICompatibleClient(api, store=store)
            start = time.perf_counter()
            result = {'sample_id': row['id'], 'question': row['input'], 'question_type': row['type'],
                      'gold_answers': row['answers'], 'method': 'direct_raw_full_context',
                      'order': 'original', 'index': index, 'run_id': run_id,
                      'status': 'OK', 'prediction': None, 'error': None}
            try:
                raw = await client.complete(run_id=run_id, interface='ANSWER', agent_role='LOW',
                    messages=[{'role': 'user', 'content': prompt(row)}])
                prediction = parse_final_answer(raw, 'boxed').answer
                if not isinstance(prediction, str) or not prediction.strip():
                    raise ValueError('empty boxed answer')
                result['prediction'] = prediction
            except Exception as exc:
                result.update(status='ERROR', error=str(exc))
            calls = [c.model_dump(mode='json') for c in store.list_model_calls(run_id)]
            store.close()
            result.update(answer_exact=answer_exact(result['prediction'], row['answers']),
                          answer_token_f1=answer_token_f1(result['prediction'], row['answers']),
                          model_calls=len(calls), latency_ms=(time.perf_counter()-start)*1000)
            trace_path = output / 'trajectories' / f"{row['id']}.json"
            trace_path.write_text(json.dumps({'result': result, 'model_calls': calls}, ensure_ascii=False, indent=2)+'\n')
            result['trajectory_path'] = str(trace_path.relative_to(ROOT))
            with (output / 'completed.jsonl').open('a') as handle:
                handle.write(json.dumps(result, ensure_ascii=False)+'\n')
            completed += 1
            line = (f"{datetime.now(timezone.utc).isoformat()} completed={completed}/{len(rows)} "
                    f"sample={row['id']} status={result['status']} exact={int(result['answer_exact'])}")
            print(line, flush=True)
            with (output / 'run.log').open('a') as handle:
                handle.write(line+'\n')
            return result

    try:
        return await asyncio.gather(*(one(i, row) for i, row in enumerate(rows)))
    finally:
        os.environ.pop('MODEL_API_KEY', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    assert config['api']['temperature'] == 0
    assert 'api_key' not in config and 'api_key' not in config['api']
    input_path = ROOT / config['input']
    assert sha(input_path) == config['expected_data_sha256']
    rows = json.loads(input_path.read_text())
    assert len(rows) == len({r['id'] for r in rows}) == 128
    for row in rows:
        assert len(re.findall(r'^Document \d+:$', row['context'], re.M)) == 50
        assert prompt(row).count(row['context']) == 1 and prompt(row).endswith(row['input']+'\n')
    tokenizer = cli._load_tokenizer(config['tokenizer_path'])
    sizes = sorted(len(tokenizer.encode(prompt(r))) for r in rows)
    preflight = {'samples': 128, 'documents': 50, 'chunking': False,
                 'temperature': 0, 'max_concurrency': config['max_concurrency'],
                 'prompt_tokens_min': sizes[0], 'prompt_tokens_median': sizes[len(sizes)//2],
                 'prompt_tokens_max': sizes[-1]}
    print(json.dumps(preflight), flush=True)
    if args.prepare_only:
        return
    output = ROOT / config['output_dir']
    if output.exists() and any(output.iterdir()):
        raise SystemExit('Output directory is not empty; choose a fresh run directory.')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'trajectories').mkdir()
    manifest = {'started_at': datetime.now(timezone.utc).isoformat(), 'sample_ids': [r['id'] for r in rows],
                'source_sha256': {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted((ROOT/'delaybind_core').glob('*.py'))},
                'script_sha256': sha(__file__), 'config_sha256': sha(args.config),
                'data_sha256': sha(input_path), 'tokenizer_sha256': sha(config['tokenizer_path']),
                'prompt_template': TEMPLATE, 'preflight': preflight,
                'logical_requests_per_question': 1, 'format_repair': False, 'memory_or_plan': False}
    (output/'code_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    (output/'config.resolved.json').write_text(json.dumps(config, ensure_ascii=False, indent=2)+'\n')
    results = asyncio.run(run(config, rows, output))
    (output/'results.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in results)+'\n')
    summary = {'count': len(results), 'correct': sum(r['answer_exact'] for r in results),
               'accuracy': sum(r['answer_exact'] for r in results)/len(results),
               'f1': sum(r['answer_token_f1'] for r in results)/len(results),
               'errors': sum(r['status']=='ERROR' for r in results),
               'finished_at': datetime.now(timezone.utc).isoformat()}
    (output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print('FINISHED', json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
