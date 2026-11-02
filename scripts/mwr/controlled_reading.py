"""Registered synthetic UPDATE diagnostics; no formal-test questions or new reader logic."""
import argparse
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
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
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.common import digest, dump
from delaybind_core.mwr.demands import reading_messages
from delaybind_core.mwr.models import Binding, InferenceInput, LiveState, Plan, Update
from delaybind_core.mwr.source_access import SourceAccess
from delaybind_core.data import CanonicalDocument

EXPLICIT = (
    'For a DORMANT demand, the unknown subject is not a reason to discard a source. '
    'Retain concise cues for each plausible entity whose explicitly stated relation matches that demand, '
    'even when that entity is not named in the original question. Keep their identities separate; '
    'the later bridge will decide which one is useful. Do not guess the bridge or broaden to unrelated relations. '
)


def fixtures(tokenizer):
    # Fixed relation families and paraphrases, constructed before any model call.
    families = [
        ('birthplace', 'Where was ?person born?', 'Where was the director of {work} born?',
         '{person} was born in {value}.', 'The birthplace of {person} is {value}.',
         '{person} lives in {value}.', 'Which person directed {work}?'),
        ('death place', 'Where did ?person die?', 'Where did the author of {work} die?',
         '{person} died in {value}.', 'The place where {person} died was {value}.',
         '{person} visited {value}.', 'Who wrote {work}?'),
        ('mother', 'Who is the mother of ?person?', 'Who is the mother of the composer of {work}?',
         'The mother of {person} is {value}.', '{person} is the child of their mother {value}.',
         '{value} is the teacher of {person}.', 'Who composed {work}?'),
        ('nationality', 'What nationality does ?person hold?', 'What nationality does the producer of {work} hold?',
         '{person} holds {value} nationality.', '{person} is a national of {value}.',
         '{person} worked in {value}.', 'Who produced {work}?'),
        ('alma mater', 'At which university did ?person graduate?', 'At which university did the designer of {work} graduate?',
         '{person} graduated from {value}.', '{value} is the university from which {person} graduated.',
         '{person} gave a talk at {value}.', 'Who designed {work}?'),
        ('employer', 'Which company employs ?person?', 'Which company employs the inventor of {work}?',
         '{person} is employed by {value}.', '{value} is the employer of {person}.',
         '{person} visited the offices of {value}.', 'Who invented {work}?'),
        ('father', 'Who is the father of ?person?', 'Who is the father of the editor of {work}?',
         'The father of {person} is {value}.', '{person} is the child of their father {value}.',
         '{value} is the colleague of {person}.', 'Who edited {work}?'),
        ('award', 'Which prize did ?person win?', 'Which prize did the illustrator of {work} win?',
         '{person} won the {value} prize.', 'The {value} prize was awarded to {person}.',
         '{person} attended the {value} prize ceremony without winning.', 'Who illustrated {work}?'),
    ]
    cases = []
    for family_index, (relation, template, question, text0, text1, distractor, parent) in enumerate(families):
        for phrasing, text in enumerate((text0, text1)):
            case_id = f'reading-{family_index:02d}-{phrasing}'
            work = f'Project-{family_index:02d}-{phrasing}'
            entities = [f'Person-{family_index:02d}-{phrasing}-{i}' for i in range(4)]
            docs = []
            for i, person in enumerate(entities):
                docs.append(CanonicalDocument(document_id=f'positive-{i}', title=f'Record-{i}',
                    sentences=[text.format(person=person, value=f'Value-{family_index:02d}-{phrasing}-{i}')]))
                docs.append(CanonicalDocument(document_id=f'other-{i}', title=f'Other-{i}',
                    sentences=[distractor.format(person=f'Other-{i}', value=f'Elsewhere-{i}')]))
            access = SourceAccess(InferenceInput(sample_id=case_id, question=question.format(work=work), documents=docs), tokenizer, neighbor_anchors=0)
            rows = access.next_window(5000)
            assert access.exhausted and len(rows) == 8
            plan = Plan(queries=[
                {'id': 'bridge', 'template': parent.format(work=work), 'output': '?person', 'relation_cues': [parent.split()[1]]},
                {'id': 'attribute', 'template': template, 'output': '?answer', 'depends_on': ['bridge'], 'relation_cues': [relation]},
            ])
            # Target position rotates independently of outputs, before execution.
            target = (family_index + phrasing) % 4
            cases.append({'id': case_id, 'relation': relation, 'phrasing': phrasing,
                          'question': question.format(work=work), 'window': rows, 'plan': plan.model_dump(),
                          'known_subject': entities[target],
                          'expected_unknown_refs': [rows[2*i]['source_ref'] for i in range(4)],
                          'expected_known_refs': [rows[2*target]['source_ref']]})
    return cases


def messages(case, variant, cfg):
    state = LiveState(plan=Plan.model_validate(case['plan']))
    if variant == 'known_subject_current':
        # Oracle readiness control only; this binding never enters the runtime.
        state.bindings['bridge'] = Binding(query_id='bridge', input_fingerprint='known-control',
            value=case['known_subject'], support_ids=[], version=1)
    settings = replace(cfg.runner.mwr, show_dormant_demands=variant != 'unknown_hide_dormant')
    out = reading_messages(case['question'], case['window'], state, settings)
    if variant == 'unknown_explicit':
        prefix, payload = out[0]['content'].rsplit('\n', 1)
        out[0]['content'] = prefix + EXPLICIT + '\n' + payload
    return out


def score(case, variant, update):
    valid = {r['source_ref'] for r in case['window']}
    emitted = {r for e in update.evidence for r in e.source_refs}
    expected = set(case['expected_known_refs' if variant == 'known_subject_current' else 'expected_unknown_refs'])
    positives = set(case['expected_unknown_refs'])
    return {'expected_source_count': len(expected), 'captured_expected': len(expected & emitted),
            'all_expected_captured': expected <= emitted, 'proposal_count': len(update.evidence),
            'emitted_source_count': len(emitted), 'non_relation_source_count': len((emitted & valid) - positives),
            'invalid_source_refs': sorted(emitted - valid), 'known_control_extra_subjects': len((emitted & positives) - expected),
            'metric_scope': 'Source pointer coverage, not cue fidelity or end-to-end accuracy'}


async def execute(args, registration, cfg, raw):
    store = SQLiteEventStore(str(args.output.with_suffix('.sqlite')))
    client = OpenAICompatibleClient(_api_config(raw), store=store)
    tokenizer = _load_tokenizer(cfg.tokenizer_path)
    results = []
    try:
        for i, condition in enumerate(registration['conditions'], 1):
            case = registration['cases'][condition['case_index']]
            run_id = f'{args.output.stem}-{i:03d}'
            calls = CallBudget(client, None, tokenizer, cfg.runner, run_id, 'controlled-reading-v1', lambda *x: None)
            result = {'case_id': case['id'], 'variant': condition['variant'], 'run_id': run_id}
            try:
                update = await calls.invoke('UPDATE', condition['messages'], Update, role='LOW')
                result.update(score(case, condition['variant'], update), response=update.model_dump())
            except Exception as exc:
                result.update(error=f'{type(exc).__name__}: {exc}', captured_expected=0,
                    expected_source_count=1 if condition['variant'] == 'known_subject_current' else 4,
                    all_expected_captured=False)
            result['usage'] = store.model_call_summary(run_id)
            results.append(result)
            with args.output.with_suffix('.jsonl').open('a') as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + '\n')
            print(f"{i}/{len(registration['conditions'])} {case['id']} {condition['variant']} capture={result['captured_expected']}/{result['expected_source_count']}", flush=True)
        groups = []
        for variant in registration['variants']:
            group = [r for r in results if r['variant'] == variant]
            groups.append({'variant': variant, 'count': len(group), 'errors': sum('error' in r for r in group),
                'captured_expected': sum(r['captured_expected'] for r in group),
                'expected_source_count': sum(r['expected_source_count'] for r in group),
                'all_expected_captured': sum(r['all_expected_captured'] for r in group),
                'non_relation_source_count': sum(r.get('non_relation_source_count', 0) for r in group),
                'invalid_source_refs': sum(len(r.get('invalid_source_refs', [])) for r in group)})
        report = {'scope': registration['scope'], 'groups': groups, 'rows': results,
                  'registration_sha256': hashlib.sha256(args.output.with_suffix('.registration.json').read_bytes()).hexdigest()}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(groups, ensure_ascii=False, indent=2))
    finally:
        store.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args()
    for suffix in ('.json', '.jsonl', '.sqlite'):
        if args.output.with_suffix(suffix).exists():
            p.error('Use a new output name; never overwrite completed failures')
    raw = json.loads(args.config.read_text())
    cfg = ExperimentConfig.from_mapping(raw)
    tokenizer = _load_tokenizer(cfg.tokenizer_path)
    cases = fixtures(tokenizer)
    variants = ['unknown_current', 'unknown_explicit', 'unknown_hide_dormant', 'known_subject_current']
    conditions = []
    for i, case in enumerate(cases):
        for variant in variants[i % 4:] + variants[:i % 4]:
            msg = messages(case, variant, cfg)
            CallBudget(None, None, tokenizer, cfg.runner, 'prepare', 'controlled-reading-v1', lambda *x: None).check_request(msg, Update)
            conditions.append({'case_index': i, 'variant': variant, 'messages': msg})
    registration = {'scope': 'Predefined synthetic diagnostic, 16 cases x 4 conditions, one run per condition. '
        'Known subject is an oracle readiness control. Unknown cases require every plausible relation source. '
        'No production prompt changes; no formal-test data; errors retained; source capture is not cue faithfulness.',
        'cases': cases, 'variants': variants, 'conditions': conditions,
        'config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted((ROOT / 'delaybind_core').rglob('*.py'))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    path = args.output.with_suffix('.registration.json')
    serialized = json.dumps(registration, ensure_ascii=False, indent=2) + '\n'
    if path.exists() and path.read_text() != serialized:
        p.error('Registered inputs changed; use a fresh output path')
    path.write_text(serialized)
    if args.prepare_only:
        print(f'Registered {len(conditions)} conditions. No model calls.')
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
