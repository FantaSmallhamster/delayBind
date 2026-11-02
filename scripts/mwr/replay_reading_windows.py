"""Paired reader-prompt diagnostic on all 28 frozen development windows."""
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
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.cli import _api_config, _load_tokenizer, _load_env_file
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.models import InferenceInput, Update
from delaybind_core.mwr.source_access import SourceAccess
from scripts.mwr.audit_pilot_capture import covered
from scripts.mwr.controlled_reading import EXPLICIT


def prepare(cfg):
    tok = _load_tokenizer(cfg.tokenizer_path)
    samples = {s.sample_id: s for s in ExperimentHarness(cfg, client_factory=lambda _: None)._samples()}
    annotation = json.loads((ROOT / 'reports/mwr/phase2_20261028/dev16_source_windows.json').read_text())
    annotation = {r['sample_id']: r for r in annotation}
    cases = []
    for path in sorted((ROOT / 'results/mwr/pilot_8k_20261028_mwr4/trajectories').glob('*-delaybind_mwr-*.json')):
        trace = json.loads(path.read_text())
        sid = trace['sample_id']
        access = SourceAccess(InferenceInput.from_sample(samples[sid]), tok, neighbor_anchors=0)
        calls = [c for c in trace['model_calls'] if c['interface'] == 'UPDATE']
        for index, call in enumerate(calls, 1):
            assert len(call['raw_request']['messages']) == 2
            user = call['raw_request']['messages'][1]
            prefix, serialized = user['content'].rsplit('\n', 1)
            payload = json.loads(serialized)
            visible = access.next_window(cfg.runner.chunk_size)
            assert payload['window'] == visible
            spans = {r['source_ref']: access.proposal_spans([r['source_ref']])[0].model_dump() for r in visible}
            support = [r for r in annotation[sid]['support_sentences'] if r['first_complete_window'] == index]
            assert all(covered(s, list(spans.values())) for s in support), 'Cross-window support needs a separate metric'
            cases.append({'sample_id': sid, 'window': index, 'source_trace': path.relative_to(ROOT).as_posix(),
                          'source_trace_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'spans': spans, 'expected_supports': support,
                          'messages': {'current': [user], 'explicit': [{'role': 'user', 'content': prefix + ' ' + EXPLICIT + '\n' + serialized}]}})
        assert access.exhausted
    assert len(cases) == 28 and sum(len(c['expected_supports']) for c in cases) == 32
    return cases


def score(case, update, tok):
    refs = {ref for e in update.evidence for ref in e.source_refs}
    valid = [case['spans'][r] for r in refs if r in case['spans']]
    flags = [covered(s, valid) for s in case['expected_supports']]
    return {'support_count': len(flags), 'support_captured': sum(flags), 'support_flags': flags,
            'invalid_source_refs': sorted(refs - case['spans'].keys()),
            'proposal_count': len(update.evidence), 'unique_source_count': len(refs),
            'proposal_serialized_tokens': len(tok.encode(update.model_dump_json()))}


async def execute(args, registration, cfg, raw):
    store = SQLiteEventStore(str(args.output.with_suffix('.sqlite')))
    client = OpenAICompatibleClient(_api_config(raw), store=store)
    tok = _load_tokenizer(cfg.tokenizer_path)
    semaphore = asyncio.Semaphore(2)
    done = 0

    async def one(index, condition):
        nonlocal done
        async with semaphore:
            case = registration['cases'][condition['case_index']]
            run_id = f'{args.output.stem}-{index:03d}'
            calls = CallBudget(client, None, tok, cfg.runner, run_id, 'frozen-reading-window-v1', lambda *x: None)
            result = {**condition, 'sample_id': case['sample_id'], 'window': case['window'], 'run_id': run_id}
            try:
                update = await calls.invoke('UPDATE', case['messages'][condition['variant']], Update, role='LOW')
                result.update(score(case, update, tok), response=update.model_dump())
            except Exception as exc:
                result.update(error=f'{type(exc).__name__}: {exc}', support_count=len(case['expected_supports']),
                              support_captured=0, support_flags=[False] * len(case['expected_supports']))
            result['usage'] = store.model_call_summary(run_id)
            with args.output.with_suffix('.jsonl').open('a') as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + '\n')
            done += 1
            print(f"{done}/{len(registration['conditions'])} {case['sample_id']} window={case['window']} {condition['variant']} capture={result['support_captured']}/{result['support_count']}", flush=True)
            return result

    try:
        rows = await asyncio.gather(*(one(i, c) for i, c in enumerate(registration['conditions'], 1)))
        groups = []
        for variant in ('current', 'explicit'):
            group = [r for r in rows if r['variant'] == variant]
            groups.append({'variant': variant, 'count': len(group), 'errors': sum('error' in r for r in group),
                **{k: sum(r.get(k, 0) for r in group) for k in ('support_count', 'support_captured', 'proposal_count', 'unique_source_count', 'proposal_serialized_tokens')},
                'invalid_source_refs': sum(len(r.get('invalid_source_refs', [])) for r in group)})
        args.output.write_text(json.dumps({'scope': registration['scope'], 'groups': groups, 'rows': rows,
            'registration_sha256': hashlib.sha256(args.output.with_suffix('.registration.json').read_bytes()).hexdigest()}, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(groups, ensure_ascii=False, indent=2), flush=True)
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args()
    if any(args.output.with_suffix(s).exists() for s in ('.json', '.jsonl', '.sqlite')):
        p.error('Use a fresh output path')
    raw = json.loads(args.config.read_text())
    cfg = ExperimentConfig.from_mapping(raw)
    cases = prepare(cfg)
    conditions = [{'case_index': i, 'variant': v} for i in range(len(cases))
                  for v in (['current', 'explicit'] if i % 2 == 0 else ['explicit', 'current'])]
    registration = {'scope': 'Paired fixed-window development diagnostic, one sample per prompt. All 16 frozen pilot questions, '
        'all 28 windows. Earlier memory and demands stay frozen from the old run, so this is not an end-to-end comparison. '
        'Official support-span coverage is positional, not semantic cue faithfulness. All four previously uncertain annotation questions remain included. '
        'No parameter changes or early stopping based on correctness; errors remain in denominators.',
        'cases': cases, 'conditions': conditions, 'max_concurrency': 2,
        'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / 'delaybind_core').rglob('*.py'))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    registration_path = args.output.with_suffix('.registration.json')
    serialized = json.dumps(registration, ensure_ascii=False, indent=2) + '\n'
    if registration_path.exists() and registration_path.read_text() != serialized:
        p.error('Registered inputs changed')
    registration_path.write_text(serialized)
    if args.prepare_only:
        print('Registered 56 conditions over 28 frozen windows. No model calls.')
        return
    _load_env_file()
    if not os.environ.get('MODEL_API_KEY'):
        os.environ['MODEL_API_KEY'] = getpass.getpass('API key (hidden, memory only): ')
    try:
        asyncio.run(execute(args, registration, cfg, raw))
    finally:
        os.environ.pop('MODEL_API_KEY', None)


if __name__ == '__main__':
    main()
