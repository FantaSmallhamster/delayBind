"""Oracle-candidate pressure control using real retention, source access and recall.

These manufactured cues isolate engineering behavior, not model extraction or
answer quality. Target identities and capacities are fixed before policy runs.
"""
import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.cli import _load_tokenizer
from delaybind_core.data import CanonicalDocument
from delaybind_core.mwr.common import digest, ProtocolError
from delaybind_core.mwr.config import MWRConfig
from delaybind_core.mwr.demands import projection
from delaybind_core.mwr.evidence import stage_proposals
from delaybind_core.mwr.models import Binding, InferenceInput, LiveState, Plan, Update
from delaybind_core.mwr.recall import select_candidates
from delaybind_core.mwr.retention import retain, tokens
from delaybind_core.mwr.source_access import SourceAccess


def fixture(seed):
    # A balanced target sweep, never chosen after observing policy performance.
    work = f'Film-{seed}'
    docs, metadata = [], {}
    for i in range(8):
        data = [
            ('dormant', f'Person-{i} was born in City-{i}.', [f'Person-{i}'], ['birthplace'], ['attribute']),
            ('current', f'The director of {work} used camera unit number {i}.', [work], ['director'], ['bridge']),
            ('unrouted', f'Archive batch {i} contains a signed production receipt.', [f'Batch-{i}'], ['archive receipt'], []),
        ]
        # Rotate presentation bucket order; target index sweeps all eight positions.
        data = data[seed % 3:] + data[:seed % 3]
        for kind, text, entities, relations, queries in data:
            title = f'{kind}-{i}'
            docs.append(CanonicalDocument(document_id=title, title=title, sentences=[text]))
            metadata[title] = {'cue_text': text, 'entity_mentions': entities, 'relation_cues': relations,
                               'candidate_query_ids': queries, 'kind': kind, 'target': kind == 'dormant' and i == seed}
    title = 'late-bridge'
    text = f'{work} was directed by Person-{seed}.'
    docs.append(CanonicalDocument(document_id=title, title=title, sentences=[text]))
    metadata[title] = {'cue_text': text, 'entity_mentions': [work, f'Person-{seed}'],
                       'relation_cues': ['director'], 'candidate_query_ids': ['bridge'], 'kind': 'bridge', 'target': False}
    plan = Plan(queries=[
        {'id': 'bridge', 'template': f'Who directed {work}?', 'output': '?person', 'relation_cues': ['director']},
        {'id': 'attribute', 'template': 'Where was ?person born?', 'output': '?city',
         'depends_on': ['bridge'], 'relation_cues': ['birthplace']},
    ])
    return InferenceInput(sample_id=f'pressure-{seed}', question=f'Where was the director of {work} born?', documents=docs), plan, metadata


def run_one(seed, policy, capacity, tokenizer):
    inp, plan, metadata = fixture(seed)
    # One complete source per forward window, using actual tokenizer costs.
    chunk_size = max(len(tokenizer.encode(d.sentences[0])) for d in inp.documents)
    cfg = MWRConfig(max_live_state_tokens=capacity, retention_policy=policy, neighbor_anchors=0,
                    max_candidate_tokens=8192, max_candidates=32)
    access = SourceAccess(inp, tokenizer, neighbor_anchors=0)
    state = LiveState(plan=plan)
    events = []
    log = lambda kind, payload: events.append({'event_type': kind, 'payload': payload})
    target_id = None
    target_span = None
    target_window = None
    target_seen = False
    ever_lost = False
    bridge_window = None
    before_bridge = None
    commits = []
    max_pre = 0
    bridge_id = None
    while not access.exhausted:
        window = access.next_window(chunk_size)
        # Targets must be completely encountered strictly before the late bridge.
        if any(row['title'] == 'late-bridge' for row in window):
            bridge_window = access.windows
            before_bridge = target_id in state.evidence
            assert target_window < bridge_window
        proposals = []
        for row in window:
            m = metadata[row['title']]
            proposals.append({'source_refs': [row['source_ref']], **{k: m[k] for k in (
                'cue_text', 'entity_mentions', 'relation_cues', 'candidate_query_ids')}})
        staged = stage_proposals(Update(evidence=proposals), state, access, tokenizer, cfg, log)
        for eid, record in staged.items():
            meta = next(m for m in metadata.values() if m['cue_text'] == record.cue_text)
            if meta['target']:
                target_id, target_span, target_window = eid, record.source_spans[0], access.windows
                target_seen = True
            if meta['kind'] == 'bridge':
                bridge_id = eid
        state.evidence.update(staged)
        max_pre = max(max_pre, tokens(state, tokenizer))
        state.version += 1
        state = retain(state, tokenizer, cfg, log)
        access.commit(state)
        assert tokens(state, tokenizer) <= capacity
        if target_seen and target_id not in state.evidence:
            ever_lost = True
            try:
                access.preview_for_evidence([target_id], state_version=state.version)
            except ProtocolError:
                pass
            else:
                raise AssertionError('Evicted evidence ID regained authorization')
        if ever_lost:
            assert target_id not in state.evidence, 'Old target was silently resurrected'
        commits.append({'window': access.windows, 'live_tokens': tokens(state, tokenizer),
                        'retained_ids': sorted(state.evidence), 'target_retained': target_id in state.evidence})
    raw = access.preview_for_evidence(sorted(state.evidence), state_version=state.version)
    reachable = any(r['doc_id'] == target_span.doc_id and r['start'] <= target_span.start and r['end'] >= target_span.end for r in raw)
    # The bridge is an oracle binding for recall inspection only, not a model judgment.
    # Charge it and its full support closure before declaring recall readiness.
    binding_kept = False
    recalled = False
    if bridge_id in state.evidence:
        state.bindings['bridge'] = Binding(query_id='bridge', input_fingerprint='oracle-bridge',
            value=f'Person-{seed}', support_ids=[bridge_id], version=state.version + 1)
        state.version += 1
        state = retain(state, tokenizer, cfg, log)
        access.commit(state)
        binding_kept = 'bridge' in state.bindings
        if binding_kept:
            demand = next(d for d in projection(state) if d['id'] == 'attribute')
            selected, _ = select_candidates(demand, state.evidence, tokenizer, cfg)
            recalled = target_id in {e.evidence_id for e in selected}
    return {'seed': seed, 'policy': policy, 'capacity': capacity,
            'windows': access.windows, 'first_complete_window': target_window, 'independent_t_star_window': bridge_window,
            'oracle_target_captured': target_seen, 'target_retained_before_bridge': before_bridge,
            'target_source_reachable_after_reading': reachable, 'oracle_bridge_retained': binding_kept,
            'target_recalled_after_budgeted_oracle_bridge': recalled,
            'eviction_events': sum(e['event_type'] == 'EVIDENCE_EVICTED' for e in events),
            'max_before_retention_tokens': max_pre, 'final_live_tokens': tokens(state, tokenizer),
            'commits': commits, 'events': events}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--prepare-only', action='store_true')
    args = p.parse_args()
    tokenizer_path = ROOT / 'models/Qwen3.5-9B-tokenizer/tokenizer.json'
    tokenizer = _load_tokenizer(str(tokenizer_path))
    capacities = [1024, 2048, 4096, 8192]
    policies = ['fifo', 'current_only', 'uniform', 'demand']
    registration = {'scope': 'Manufactured oracle proposals, fixed balanced target positions; engineering pressure control only. '
                    'No model extraction or final answer scores. t-star comes from the last explicit bridge source, not runtime BOUND.',
                    'capacities': capacities, 'policies': policies, 'seeds': list(range(8)),
                    'fixtures': [{'input': fixture(i)[0].model_dump(), 'plan': fixture(i)[1].model_dump(), 'metadata': fixture(i)[2]} for i in range(8)],
                    'tokenizer_sha256': hashlib.sha256(tokenizer_path.read_bytes()).hexdigest(),
                    'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                      for p in sorted((ROOT / 'delaybind_core/mwr').glob('*.py'))}}
    path = args.output.with_suffix('.registration.json')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(registration, ensure_ascii=False, indent=2) + '\n'
    if path.exists() and path.read_text() != serialized:
        p.error('Registered control changed; use a new output path')
    if args.output.exists():
        p.error('Output already exists')
    path.write_text(serialized)
    if args.prepare_only:
        print('Registered 128 deterministic controls; zero model calls.')
        return
    rows = [run_one(seed, policy, capacity, tokenizer) for capacity in capacities for policy in policies for seed in range(8)]
    groups = []
    for capacity in capacities:
        for policy in policies:
            group = [r for r in rows if r['capacity'] == capacity and r['policy'] == policy]
            groups.append({'capacity': capacity, 'policy': policy, 'count': len(group),
                **{key: sum(r[key] for r in group) for key in (
                    'target_retained_before_bridge', 'target_source_reachable_after_reading',
                    'target_recalled_after_budgeted_oracle_bridge', 'eviction_events')}})
    args.output.write_text(json.dumps({'registration_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'scope': registration['scope'], 'groups': groups, 'rows': rows}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
