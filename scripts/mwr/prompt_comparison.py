"""Fixed-count real-model prompt diagnostics; gold stays outside requests."""
import argparse
import asyncio
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.cli import _api_config, _load_env_file, _load_tokenizer
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr import PROMPT_VERSION
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.binding import assessment_messages
from delaybind_core.mwr.answering import answer_request, AnswerContract
from delaybind_core.mwr.models import Assessment, Answer
from delaybind_core.mwr.scoring import normalize


def current_messages(case):
    payload = case['payload']
    return (assessment_messages(payload) if case['kind'] == 'assessment' else
            answer_request(payload['question'], payload['evidence_bundle'], AnswerContract()))


def evaluate(case, parsed):
    value = parsed.value if case['kind'] == 'assessment' else parsed.answer
    correct = parsed.status == case['expected_status']
    if parsed.status in {'SUPPORTED', 'ANSWERED'}:
        correct = correct and normalize(value) == normalize(case['expected_value'])
    if case['kind'] == 'assessment':
        valid = parsed.input_fingerprint == case['payload']['input_fingerprint']
        valid = valid and set(parsed.support_ids) <= {e['evidence_id'] for e in case['payload']['candidates']}
        expected = case.get('expected_faithful')
        fidelity = parsed.faithful == expected if expected is not None else None
    else:
        valid = set(parsed.source_refs) <= {s['source_ref'] for s in case['payload']['evidence_bundle']['raw_sources']}
        fidelity = None
    return {'correct': bool(correct and valid), 'contract_valid': bool(valid), 'fidelity_correct': fidelity}


async def run(args, cases, registration, cfg, raw):
    store = SQLiteEventStore(str(args.output.with_suffix('.sqlite')))
    client = OpenAICompatibleClient(_api_config(raw), store=store)
    tokenizer = _load_tokenizer(cfg.tokenizer_path)
    results = []
    try:
        for number, condition in enumerate(registration['conditions'], 1):
            case = cases[condition['case_index']]
            # Each condition has an independent run ID: repeats cannot hit an old response cache.
            run_id = args.output.stem + '-' + str(number)
            calls = CallBudget(client, None, tokenizer, cfg.runner, run_id, 'prompt-comparison-v1', lambda *x: None)
            answer_case = case['kind'] == 'answer'
            row = {**condition, 'case_id': case['id'], 'kind': case['kind'], 'run_id': run_id}
            try:
                parsed = await calls.invoke('ANSWER' if answer_case else 'MEMORY', condition['messages'],
                                            Answer if answer_case else Assessment, final=answer_case,
                                            role='LOW' if answer_case else 'HIGH')
                row.update(evaluate(case, parsed), response=parsed.model_dump())
            except Exception as exc:
                row.update(correct=False, contract_valid=False, fidelity_correct=None,
                           error_type=type(exc).__name__, error=str(exc))
            row['usage'] = store.model_call_summary(run_id)
            # Messages are already in the registration and SQLite request ledger.
            row.pop('messages')
            results.append(row)
            with args.output.with_suffix('.jsonl').open('a') as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            print(f"{number}/{len(registration['conditions'])} {case['kind']} {case['id']} {condition['variant']} repeat={condition['repeat']} correct={row['correct']}", flush=True)
        groups = []
        for variant in registration['variants']:
            for kind in ('assessment', 'answer'):
                rows = [r for r in results if r['variant'] == variant and r['kind'] == kind]
                fidelity = [r for r in rows if r['fidelity_correct'] is not None]
                supported = [r for r in rows if r.get('response', {}).get('status') in {'SUPPORTED', 'ANSWERED'}]
                if rows:
                    groups.append({'variant': variant, 'kind': kind, 'count': len(rows),
                        'correct': sum(r['correct'] for r in rows), 'supported': len(supported),
                        'incorrect_supported': sum(not r['correct'] for r in supported),
                        'fidelity_count': len(fidelity), 'fidelity_correct': sum(r['fidelity_correct'] for r in fidelity)})
        report = {'diagnostic_only': True, 'prompt_version': PROMPT_VERSION, 'groups': groups, 'rows': results,
                  'registration_sha256': hashlib.sha256(args.output.with_suffix('.registration.json').read_bytes()).hexdigest()}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(groups, ensure_ascii=False, indent=2), flush=True)
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases', type=Path, required=True)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--repeats', type=int, default=2)
    p.add_argument('--variants', nargs='+', choices=['old', 'current'], default=['old', 'current'])
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args()
    if args.repeats < 1:
        p.error('repeats must be positive')
    if any(args.output.with_suffix(s).exists() for s in ('.json', '.jsonl', '.sqlite')):
        p.error('use a fresh output path; completed failures must not be overwritten')
    cases = json.loads(args.cases.read_text())
    raw = json.loads(args.config.read_text())
    cfg = ExperimentConfig.from_mapping(raw)
    conditions = []
    for repeat in range(1, args.repeats + 1):
        # Alternate order to reduce confounding by a monotonic provider change.
        variants = args.variants if repeat % 2 else list(reversed(args.variants))
        for index, case in enumerate(cases):
            for variant in variants:
                messages = case['old_messages'] if variant == 'old' else current_messages(case)
                conditions.append({'case_index': index, 'repeat': repeat, 'variant': variant, 'messages': messages})
    registration = {'cases_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / 'delaybind_core').rglob('*.py'))},
        'conditions': conditions, 'variants': args.variants,
        'scope': 'Fixed-count development diagnostics, same client/schema/settings. Legacy versus current user prompt; shared current system wrapper. No early stopping by correctness; API failures retained.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    registration_path = args.output.with_suffix('.registration.json')
    serialized = json.dumps(registration, ensure_ascii=False, indent=2) + '\n'
    if registration_path.exists() and registration_path.read_text() != serialized:
        p.error('pre-registered contract changed; use a new output path')
    registration_path.write_text(serialized)
    if args.prepare_only:
        print(f'Registered {len(conditions)} fixed conditions. No model calls.')
        return
    _load_env_file()
    if not os.environ.get('MODEL_API_KEY'):
        os.environ['MODEL_API_KEY'] = getpass.getpass('API key (hidden, memory only): ')
    try:
        asyncio.run(run(args, cases, registration, cfg, raw))
    finally:
        os.environ.pop('MODEL_API_KEY', None)


if __name__ == '__main__':
    main()
