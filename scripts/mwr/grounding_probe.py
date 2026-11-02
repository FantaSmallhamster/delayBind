"""Diagnostic-only one-call quotation-first output; never a runtime fallback."""
import argparse
import asyncio
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from pydantic import Field
from delaybind_core.mwr.models import Strict, Assessment, Answer
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.common import ProtocolError
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.cli import _api_config, _load_env_file, _load_tokenizer
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.storage import SQLiteEventStore
from scripts.mwr.prompt_comparison import current_messages, evaluate


class Quotation(Strict):
    source_ref: str
    quote: str = Field(min_length=1, max_length=320, description='Exact contiguous quotation from displayed raw text, never from an inaccurate cue.')


class GroundedAssessment(Strict):
    grounding: list[Quotation] = Field(max_length=8, description='Relevant raw quotations, with subject and object intact, collected before judging the result.')
    result: Assessment


class GroundedAnswer(Strict):
    grounding: list[Quotation] = Field(max_length=8, description='Relevant raw quotations needed to connect the requested answer.')
    result: Answer


async def run(args, cases, cfg, raw):
    store = SQLiteEventStore(str(args.output.with_suffix('.sqlite')))
    client = OpenAICompatibleClient(_api_config(raw), store=store)
    tokenizer = _load_tokenizer(cfg.tokenizer_path)
    rows = []
    try:
        for index, case in enumerate(cases):
            calls = CallBudget(client, None, tokenizer, cfg.runner, args.output.stem + '-' + str(index), 'quotation-probe-v1', lambda *x: None)
            answer_case = case['kind'] == 'answer'
            messages = current_messages(case)
            messages[0]['content'] = ('Return grounding first, then result. Grounding contains only short exact quotations from raw_sources, preserving who does what to whom. '
                'These quotations are source evidence, not reasoning or cue paraphrases. Use them to decide the result independently of candidate cues. '
                'Put the requested judgment inside result, with all its fields.\n' + messages[0]['content'])
            sources = case['payload']['evidence_bundle']['raw_sources'] if answer_case else case['payload']['raw_sources']
            shown = {s['source_ref']: s['text'] for s in sources}
            def validate(parsed):
                if any(q.source_ref not in shown or q.quote not in shown[q.source_ref] for q in parsed.grounding):
                    raise ProtocolError('grounding must quote a displayed source exactly')
                if parsed.result.status in {'SUPPORTED', 'ANSWERED'} and not parsed.grounding:
                    raise ProtocolError('supported result requires at least one raw quotation')
                return parsed
            row = {'case_id': case['id'], 'kind': case['kind'], 'run_id': calls.run_id}
            try:
                parsed = await calls.invoke('ANSWER' if answer_case else 'MEMORY', messages,
                    GroundedAnswer if answer_case else GroundedAssessment, final=answer_case, validator=validate)
                row.update(evaluate(case, parsed.result), response=parsed.model_dump())
            except Exception as exc:
                row.update(correct=False, contract_valid=False, fidelity_correct=None,
                           error_type=type(exc).__name__, error=str(exc))
            row['usage'] = store.model_call_summary(calls.run_id)
            rows.append(row)
            with args.output.with_suffix('.jsonl').open('a') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(f"{index+1}/{len(cases)} {case['id']} correct={row['correct']}", flush=True)
        report = {'diagnostic_only': True, 'not_runtime_fallback': True, 'count': len(rows), 'correct': sum(r['correct'] for r in rows), 'rows': rows}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print('Quotation-first diagnostic:', report['correct'], '/', report['count'], flush=True)
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases', type=Path, nargs='+', required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if any(a.output.with_suffix(s).exists() for s in ('.json', '.jsonl', '.sqlite')):
        p.error('use a fresh output path')
    cases = [r for path in a.cases for r in json.loads(path.read_text())]
    raw = json.loads(a.config.read_text()); cfg = ExperimentConfig.from_mapping(raw)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    registration = {'count': len(cases), 'repeats': 1, 'case_files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a.cases},
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'config_sha256': hashlib.sha256(a.config.read_bytes()).hexdigest(),
        'source_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('delaybind_core').rglob('*.py')},
        'scope': 'All 43 previously observed diagnostic cases; exploratory development, not independent holdout. One model call plus bounded format repair, no extra retrieval or truth oracle.'}
    a.output.with_suffix('.registration.json').write_text(json.dumps(registration, ensure_ascii=False, indent=2) + '\n')
    _load_env_file()
    if not os.environ.get('MODEL_API_KEY'):
        os.environ['MODEL_API_KEY'] = getpass.getpass('API key (hidden, memory only): ')
    try:
        asyncio.run(run(a, cases, cfg, raw))
    finally:
        os.environ.pop('MODEL_API_KEY', None)


if __name__ == '__main__':
    main()
