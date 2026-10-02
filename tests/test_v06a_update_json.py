"""Strict JSON UPDATE adapter, bounded recovery and actual API bookkeeping."""

import asyncio
import json

import pytest

from delaybind_core.agent_prompts import final_answer_prompt, reading_json_prompt, reading_json_repair_prompt
from delaybind_core.api import APIConfig, OpenAICompatibleClient
from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.source_refs import VisibleSources
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import run_subqueries
from delaybind_core.update_json import parse_update_json, UpdateJSONError, update_response_format


def fact(query="Q1", source="s1", text="Ada directed Fern."):
    return {"query": query, "sources": [source], "text": text}


def envelope(facts=None, hints=None):
    return json.dumps({"facts": facts if facts is not None else [fact()], "hints": hints or []})


def test_json_preserves_natural_language_and_hint_content():
    text = 'Ada wrote "A|B".\n她出生于 Rome；路径 C:\\docs。'
    parsed = parse_update_json(envelope([fact(text=text)], [{"sources": ["s1"], "text": "Need a school query."}]))
    assert parsed.facts[0].text == text
    assert set(parsed.facts[0].model_dump()) == {"query_id", "source_refs", "text"}
    assert len(parsed.hints) == 1


@pytest.mark.parametrize("raw", [
    '{"facts":[{"query":"Q1","sources":["s1"],"text":"fact"}],"hints":',
    'NONE', '[]', '{"facts":[]}', '{"facts":null,"hints":[]}',
    '{"facts":[],"hints":[],"bindings":[]}',
    '{"facts":[],"facts":[],"hints":[]}',
    '{"facts":[{"query":"Q1","query":"Q2","sources":["s1"],"text":"fact"}],"hints":[]}',
    '{"facts":NaN,"hints":[]}', '```json\n{"facts":[],"hints":[]}\n```',
    '{"facts":[],"hints":[]} extra prose',
])
def test_ambiguous_or_incomplete_envelope_is_not_salvaged(raw):
    with pytest.raises(UpdateJSONError):
        parse_update_json(raw)


@pytest.mark.parametrize("bad", [
    {"query": "Q1", "sources": ["s1"], "text": "bad", "relevance": "ACTIVE"},
    {"query_id": "Q1", "sources": ["s1"], "text": "bad"},
    {"query": "Q1", "sources": "s1", "text": "bad"},
    {"query": "Q1", "sources": [], "text": "bad"},
    {"query": "Q1", "sources": ["NONE"], "text": "bad"},
    {"query": 1, "sources": ["s1"], "text": "bad"},
    {"query": "Q1", "sources": [1], "text": "bad"},
    {"query": "Q1", "sources": ["s1"], "text": False},
    {"query": "Q1", "sources": ["s1"], "text": " "},
    {"query": "Q1", "sources": ["s1"]}, None,
])
def test_bad_item_cannot_coerce_values_or_discard_independent_fact(bad):
    parsed = parse_update_json(envelope([fact(), bad]))
    assert [f.text for f in parsed.facts] == ["Ada directed Fern."]
    assert len(parsed.rejected_lines) == 1


def test_local_query_and_source_allowlists_include_hints():
    sources = VisibleSources([{"source_ref": "s1", "text": "Ada directed Fern."}])
    parsed = parse_update_json(envelope([fact(), fact(query="Q99"), fact(source="future"),
                                         fact(source="F123")], [{"sources": ["future"], "text": "bad hint"}]),
                               sources=sources, query_ids={"Q1"})
    assert len(parsed.facts) == 1
    assert len(parsed.rejected_lines) == 4
    assert not parsed.hints


def test_json_config_and_prompts_have_one_contract_and_no_state_field():
    config = ExperimentConfig.from_mapping({"input": "unused", "runner": {
        "update_protocol": "json", "update_json_mode": "json_schema"}})
    assert config.runner.update_protocol == "json"
    assert RunnerConfig().update_protocol == "text"
    with pytest.raises(ValueError):
        RunnerConfig(update_json_mode="automatic_fallback")
    for prompt in [reading_json_prompt("question", [], []), reading_json_repair_prompt(
        "question", [], [], rejected_lines=[], memory="", source_refs=[], whole_response=False)]:
        assert 'JSON object' in prompt
        assert 'three fields' not in prompt
        assert 'Output only these short lines' not in prompt
        assert '"relevance"' not in prompt
        assert 'hints' in prompt
        assert 'query assignment is genuinely uncertain' in prompt or 'cannot\nsafely determine which one existing query ID' in prompt
    answer_prompt = final_answer_prompt(
        "Where was Ada born?", "F1 | s1 | Ada was born in Rome.",
        answer_format="boxed",
        hints=[{"fact_id": "F2", "source_refs": ["s2"], "text": "Ada later lived in Paris."}],
    )
    assert 'Source-backed hints not yet attached to a specific query' in answer_prompt
    assert 'Ada later lived in Paris.' in answer_prompt
    assert update_response_format("json_schema")["json_schema"]["strict"] is True
    assert update_response_format("json_object") == {"type": "json_object"}
    assert update_response_format("prompt") is None


class FixtureClient(OpenAICompatibleClient):
    def __init__(self, store, case):
        super().__init__(APIConfig(base_url="http://unused", api_key="test", model="mock", max_retries=0), store=store)
        self.case, self.updates, self.requests = case, 0, []

    def _call_sync(self, payload):
        self.requests.append(payload)
        prompt = payload['messages'][0]['content']
        finish, refusal = 'stop', None
        if prompt.startswith('<UPDATE'):
            self.updates += 1
            refs = {p['title']: p['source_ref'] for p in (json.loads(r[0]) for r in self.store.connection.execute(
                "select payload_json from raw_archive where run_id='json-fixture'"))}
            director = fact(source=refs['Fern'])
            child = fact(query="Q2", source=refs['Ada'], text="Ada was born in Rome.") if 'Ada' in refs else None
            if self.case == 'late':
                content = envelope([director] if self.updates == 1 else [child])
            elif self.case == 'repair' and self.updates == 1:
                content = envelope([director, {**child, "sources": ["future"]}])
            elif self.case == 'repair':
                content = envelope([child])
            elif self.case == 'exhausted':
                content = envelope(([director] if self.updates == 1 else []) + [{**child, "relevance": "DORMANT"}])
            elif self.case == 'truncated' and self.updates == 1:
                content = '{"facts":[' + json.dumps(director)
                finish = 'length'
            elif self.case == 'syntax' and self.updates == 1:
                content = '{"facts":[' + json.dumps(director)
            elif self.case == 'refusal' and self.updates == 1:
                content = None
                refusal = 'fixture refusal'
            elif self.case == 'all_invalid':
                content = '{"facts":['
            elif self.case == 'hint_answer':
                content = envelope([], [{"sources": [refs['Ada']], "text": "Ada was born in Rome."}])
            elif self.case == 'empty':
                content = envelope([])
            else:
                content = envelope([director, child])
        elif prompt.startswith('<MEMORY_GROUNDED'):
            rows = json.loads(prompt.split('Facts to check (use allowed_context_refs for citations):\n', 1)[1]
                              .split('\n\nOriginal source excerpts:', 1)[0])
            checks = [f"{r['query_id']} | {r['fact_id']} | SUPPORTED | {r['allowed_target_refs'][0]} | faithful" for r in rows]
            bindings = []
            for qid, value, target in [('Q1', 'Ada', 'Ada directed Fern.'), ('Q2', 'Rome', 'Ada was born in Rome.')]:
                row = next((r for r in rows if r['query_id'] == qid and r['fact_text'] == target), None)
                if row and f'{qid} [ACTIVE]' in prompt:
                    bindings.append(f"BIND | {qid} | {value} | {row['fact_id']}")
            content = 'CHECKS\n' + '\n'.join(checks) + '\nBINDINGS\n' + ('\n'.join(bindings) or 'NONE')
        elif prompt.startswith('<RECALL'):
            content = prompt.split('Selectable candidate IDs: ', 1)[1].splitlines()[0]
        elif prompt.startswith('<ANSWER'):
            content = r'\boxed{Rome}'
        else:
            raise AssertionError(prompt[:50])
        return {'choices': [{'finish_reason': finish, 'message': {'content': content, 'refusal': refusal}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 10}}, 1.0


def run_case(case, mode='json_schema'):
    store = SQLiteEventStore()
    client = FixtureClient(store, case)
    plan = QueryPlan(plan_id='json-fixture', queries=[
        Subquery(id='Q1', template='Who directed Fern?', output='?director'),
        Subquery(id='Q2', template='Where was ?director born?', output='?birthplace', depends_on=['Q1']),
    ])
    sample = CanonicalSample(sample_id='json-fixture', question='Where was the director of Fern born?', documents=[
        CanonicalDocument(document_id='d1', title='Fern', sentences=['Ada directed Fern.']),
        CanonicalDocument(document_id='d2', title='Ada', sentences=['Ada was born in Rome.']),
    ])
    try:
        result = asyncio.run(run_subqueries(client=client, sample=sample, plan=plan, store=store, run_id='json-fixture',
            config=RunnerConfig(answer_format='boxed', snapshot_every_windows=0,
                memory_source_mode='joint_source_memory', update_protocol='json', update_json_mode=mode,
                chunk_size=3 if case == 'late' else 5000,
                max_response_retries=3)))
        events, calls = store.list_runtime_events('json-fixture'), store.list_model_calls('json-fixture')
        return result, events, calls, client
    finally:
        store.close()


@pytest.mark.parametrize('mode', ['json_schema', 'json_object', 'prompt'])
def test_transport_is_frozen_to_update_only(mode):
    result, events, calls, client = run_case('valid', mode)
    assert result['state']['bindings'] == {'?director': 'Ada', '?birthplace': 'Rome'}
    assert client.updates == 1
    for call in calls:
        expected = update_response_format(mode) if call.interface == 'UPDATE' else None
        assert call.raw_request.get('response_format') == expected
    deferred = [e for e in events if e.event_type == 'FACT_DEFERRED']
    assert len(deferred) == 1 and deferred[0].payload['query_status'] == 'DORMANT'
    assert [c.interface for c in calls] == ['UPDATE', 'MEMORY_GROUNDED', 'RECALL', 'MEMORY_GROUNDED', 'ANSWER']


def test_partial_repair_retains_valid_parent_and_repairs_only_rejected_child():
    result, events, calls, client = run_case('repair')
    assert client.updates == 2
    assert result['state']['bindings'] == {'?director': 'Ada', '?birthplace': 'Rome'}
    update_calls = [c for c in calls if c.interface == 'UPDATE']
    assert 'Repair ONLY the rejected facts/hints' in update_calls[1].raw_request['messages'][0]['content']
    assert len([e for e in events if e.event_type == 'FACT_STORED']) == 2
    assert len([e for e in events if e.event_type == 'UPDATE_JSON_REJECTED']) == 1


@pytest.mark.parametrize('case,category', [('truncated', 'TRUNCATED'), ('syntax', 'JSON_SYNTAX'), ('refusal', 'REFUSAL')])
def test_full_response_errors_get_one_explicit_repair(case, category):
    result, events, calls, client = run_case(case)
    assert client.updates == 2
    assert result['state']['bindings']['?birthplace'] == 'Rome'
    errors = [e for e in events if e.event_type == 'UPDATE_JSON_REJECTED']
    assert errors[0].payload['category'] == category
    repair = next(e for e in events if e.event_type == 'UPDATE_REPAIR_REQUESTED')
    assert repair.payload['retained_facts'] == 0


def test_exhaustion_preserves_good_items_and_marks_failure():
    result, events, calls, client = run_case('exhausted')
    assert client.updates == 2  # max_response_retries=3 cannot exceed one JSON repair.
    assert result['state']['bindings'] == {'?director': 'Ada'}
    assert result['protocol_valid'] is False
    assert any(e.event_type == 'PROTOCOL_REPAIR_EXHAUSTED' for e in events)
    assert len([e for e in events if e.event_type == 'FACT_STORED']) == 1


def test_total_syntax_failure_is_not_reported_as_a_valid_empty_response():
    result, events, calls, client = run_case('all_invalid')
    assert client.updates == 2
    assert result['protocol_valid'] is False
    assert not result['state']['facts']
    assert not any(e.event_type == 'UPDATE_JSON_PARSED' for e in events)


def test_empty_facts_are_valid_when_explicitly_returned_by_model():
    result, events, calls, client = run_case('empty')
    assert client.updates == 1
    assert result['protocol_valid'] is True
    assert not result['state']['facts']
    assert any(e.event_type == 'UPDATE_JSON_PARSED' and e.payload['facts'] == 0 for e in events)


def test_answer_prompt_receives_saved_source_backed_hints():
    result, events, calls, client = run_case('hint_answer')
    answer_call = next(c for c in calls if c.interface == 'ANSWER')
    prompt = answer_call.raw_request['messages'][0]['content']
    assert 'Source-backed hints not yet attached to a specific query' in prompt
    assert 'Ada was born in Rome.' in prompt


def test_late_json_fact_after_empty_review_keeps_runtime_routing():
    result, events, calls, client = run_case('late')
    assert client.updates == 2
    assert result['state']['bindings'] == {'?director': 'Ada', '?birthplace': 'Rome'}
    empty = next(e for e in events if e.event_type == 'DEFER_BUCKET_EMPTY')
    admitted = next(e for e in events if e.event_type == 'FACT_COMMITTED' and e.payload['use']['query_id'] == 'Q2')
    assert empty.event_seq < admitted.event_seq
    assert admitted.payload['query_status'] == 'ACTIVE'
    assert all(c.interface != 'RECALL' for c in calls)
