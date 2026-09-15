"""V5.1 acceptance cases, including the user's confirmed document overrides."""

import asyncio
import json
import re

import pytest

from delaybind_core import RawArchive, RunnerConfig, SQLiteEventStore, V5Runner, build_manifest, canonicalize_record
from delaybind_core.agents import HighLevelAgent, LowLevelAgent
from delaybind_core.cursor import TextReadCursor
from delaybind_core.fact_protocol import (BindingProposal, FactEvent, FactUpdate, ProtocolError,
                                         parse_judgments, parse_memory, parse_plan, parse_selection, parse_update)
from delaybind_core.metrics import score_result, summarize_results
from delaybind_core.plan_validation import PlanValidationError, ensure_valid_plan
from delaybind_core.replay import replay_events
from delaybind_core.schema import QueryPlan
from delaybind_core.subqueries import SubqueryRuntime


PLAN = """Q1
query: Who is Cindy's teacher?
output: ?teacher
depends_on: NONE

Q2
query: Who is ?teacher's mother?
output: ?mother
depends_on: Q1

Q3
query: Where was ?mother born?
output: ?city
depends_on: Q2"""


def record():
    return {"_id": "example", "question": "Where was Cindy's teacher's mother born?", "answer": "Suzhou",
            "context": [["Mary", ["Mary was born in Suzhou."]],
                        ["Alice", ["Alice's mother is Mary."]],
                        ["Cindy", ["Cindy's teacher is Alice."]]],
            "supporting_facts": [["Mary", 0], ["Alice", 0], ["Cindy", 0]]}


def section(prompt, title, next_title):
    return prompt.split(title + ':\n', 1)[1].split('\n\n' + next_title, 1)[0]


def facts_in(text):
    facts = {}
    for line in text.splitlines():
        if re.match(r'^F[0-9a-f]+ \|', line):
            fid, refs, fact = line.split(' | ', 2)
            facts[fid] = (refs.split(','), fact)
    return facts


class ScriptedClient:
    def __init__(self, *, role=None, timeline=None, reverse_events=False, plan_retry=False):
        self.role = role
        self.timeline = timeline if timeline is not None else []
        self.reverse_events = reverse_events
        self.plan_retry = plan_retry
        self.plan_calls = 0

    async def complete(self, *, interface, agent_role, messages, response_schema=None, **kwargs):
        if self.role:
            assert agent_role == self.role
        prompt = messages[0]['content']
        self.timeline.append((agent_role, interface, prompt))
        if interface == 'PLAN':
            self.plan_calls += 1
            assert 'Suzhou' not in prompt and 'Mary' not in prompt
            return 'invalid' if self.plan_retry and self.plan_calls == 1 else PLAN
        if interface == 'UPDATE':
            text = section(prompt, 'Current window', 'Output only')
            plan = section(prompt, 'Plan', 'Working memory:')
            sources = re.findall(r'\[([^\]]+)\][^\n]*\n(.*?)(?=\n\n\[|$)', text, re.S)
            rows = []
            for ref, sentence in sources:
                sentence = sentence.strip()
                qid = 'Q1' if 'teacher' in sentence else 'Q2' if 'mother' in sentence else 'Q3'
                status = re.search(rf'{qid} \[([^\]]+)\]', plan)[1]
                rows.append(f'{qid} | {ref} | {sentence} | ' + ('DORMANT' if status == 'DORMANT' else 'ACTIVE'))
            return '\n'.join(reversed(rows) if self.reverse_events else rows) or 'NONE'
        if interface == 'MEMORY':
            plan = section(prompt, 'Plan', 'Working memory:')
            memory = facts_in(section(prompt, 'Working memory', 'Saved plan hints:'))
            commands = []
            for qid, needle, answer in [('Q1', "Cindy's teacher is Alice.", 'Alice'),
                                        ('Q2', "Alice's mother is Mary.", 'Mary'),
                                        ('Q3', 'Mary was born in Suzhou.', 'Suzhou')]:
                if f'{qid} [ACTIVE]' not in plan:
                    continue
                match = next((fid for fid, (_, text) in memory.items() if text == needle), None)
                if match:
                    commands.append(f'BIND | {qid} | {answer} | {match}')
            return '\n'.join(commands) or 'NONE'
        if interface == 'RECALL':
            candidates = facts_in(section(prompt, 'Deferred candidates', 'Return SELECT'))
            assert candidates
            return 'SELECT | ' + ','.join(candidates)
        if interface == 'VERIFY':
            candidates = facts_in(section(prompt, 'Selected deferred candidates', 'Their already-read original sources:'))
            assert 'Q1 [ACTIVE]' not in section(prompt, 'Query', 'Binding support:')
            return '\n'.join(f'{fid} | MATCH' for fid in candidates)
        if interface == 'ANSWER':
            memory = facts_in(section(prompt, 'Working memory', 'Return JSON' if response_schema else 'Put the final'))
            refs = list(dict.fromkeys(ref for source_refs, _ in memory.values() for ref in source_refs))
            if response_schema:
                return json.dumps({'answer': 'Suzhou', 'source_refs': refs})
            return r'\boxed{Suzhou}'
        raise AssertionError(interface)


def run_chain(*, order='original', chunk_size=1, client=None, **config):
    sample = canonicalize_record(record())
    store = SQLiteEventStore()
    client = client or ScriptedClient()
    result = asyncio.run(V5Runner(client, config=RunnerConfig(chunk_size=chunk_size, **config)).run(
        run_id='chain', sample=sample, manifest=build_manifest(sample, order=order), store=store))
    return result, store, client, sample


@pytest.mark.parametrize('order,verifications', [('original', 2), ('reverse', 0)])
def test_reverse_callbacks_and_forward_active_commits_use_distinct_paths(order, verifications, monkeypatch):
    monkeypatch.setattr(RawArchive, 'search_mentions', lambda *args, **kwargs: pytest.fail('free archive search'))
    monkeypatch.setattr(RawArchive, 'expanded_context', lambda *args, **kwargs: pytest.fail('whole archive expansion'))
    result, store, client, sample = run_chain(order=order)
    assert result['state']['bindings'] == {'?teacher': 'Alice', '?mother': 'Mary', '?city': 'Suzhou'}
    assert result['evidence_pack']['answer_value'] == 'Suzhou'
    assert result['interface_calls'].get('VERIFY', 0) == verifications
    assert result['interface_calls']['UPDATE'] == result['windows_processed'] == 3
    assert result['interface_calls']['ANSWER'] == 1
    assert all(role == ('HIGH' if interface in {'PLAN', 'MEMORY', 'ANSWER'} else 'LOW')
               for role, interface, _ in client.timeline)
    assert all(q['status'] == 'RESOLVED' for q in result['state']['queries'])
    assert len(result['state']['facts']) == 3
    assert len(result['state']['working_memory']['facts']) == 3
    assert len(result['state']['working_memory']['links']) == 2
    assert len(result['evidence_pack']['links']) == 2
    assert all(not ids for ids in result['state']['defer_workspace'].values())
    events = store.list_runtime_events('chain')
    assert replay_events(events).export() == result['state']
    types = [event.event_type for event in events]
    assert 'SUFFICIENCY_CHECKED' not in types
    if verifications:
        assert types.index('DEFER_CANDIDATES_SELECTED') < types.index('DEFER_SOURCES_FETCHED') < types.index('FACT_PROMOTED')
    assert not any('TARGETED_UPDATE' in name for name in types)
    scored = score_result({**result, 'status': 'OK', 'events': [event.model_dump(mode='json') for event in events]}, sample)
    assert scored['answer_exact'] and scored['supporting_f1'] == 1
    assert scored['graph_triple_f1'] is None
    assert scored['cross_window_deferred_promoted_count'] == verifications
    assert summarize_results([scored])['groups'][0]['answer_accuracy'] == 1


def test_same_window_registers_every_fact_before_binding_independent_of_output_order():
    a, _, _, _ = run_chain(chunk_size=1000)
    b, _, _, _ = run_chain(chunk_size=1000, client=ScriptedClient(reverse_events=True))
    assert a['state']['bindings'] == b['state']['bindings']
    assert {fact['fact_id'] for fact in a['evidence_pack']['facts']} == {fact['fact_id'] for fact in b['evidence_pack']['facts']}
    assert a['windows_processed'] == b['windows_processed'] == 1
    assert a['interface_calls']['VERIFY'] == b['interface_calls']['VERIFY'] == 2


def runtime_fixture(plan=None):
    sample = canonicalize_record(record())
    manifest = build_manifest(sample)
    store = SQLiteEventStore()
    archive = RawArchive(store, 'unit')
    archive.append(manifest.entries)
    runtime = SubqueryRuntime(run_id='unit', plan=plan or parse_plan(PLAN), archive=archive, store=store)
    return runtime, manifest, store


def ingest(runtime, entry, qid, relevance='ACTIVE', *, window=0):
    runtime.ingest(FactUpdate(facts=[FactEvent(query_id=qid, text=entry.text,
                   source_refs=[entry.source_ref], relevance=relevance)]),
                   window_index=window, allowed_refs={entry.source_ref})
    return next(fid for fid, fact in runtime.state.facts.items() if fact.text == entry.text)


def bind(runtime, qid, value, fid, *, window=0):
    return runtime.apply_binding(BindingProposal(query_id=qid, value=value, support_refs=[fid]), window_index=window)


def test_active_commit_needs_no_verify_but_does_not_itself_resolve_query():
    runtime, manifest, _ = runtime_fixture()
    fid = ingest(runtime, manifest.entries[2], 'Q1')
    assert fid in runtime.state.accepted_ids()
    assert runtime.state.executions['Q1'].status == 'ACTIVE'
    assert runtime.state.bindings == {}
    bind(runtime, 'Q1', 'Alice', fid)
    assert runtime.state.bindings == {'?teacher': 'Alice'}
    assert runtime.state.query_projection('Q2')['rendered_query'] == "Who is Alice's mother?"


def test_one_fact_has_independent_query_uses_and_deduplicated_body():
    runtime, manifest, _ = runtime_fixture()
    fid = ingest(runtime, manifest.entries[1], 'Q1')
    same = ingest(runtime, manifest.entries[1], 'Q2', 'DORMANT')
    ingest(runtime, manifest.entries[1], 'Q2', 'DORMANT')
    assert fid == same and len(runtime.state.facts) == 1
    assert runtime.state.uses['Q1'][fid].status == 'ACCEPTED'
    assert runtime.state.uses['Q2'][fid].status == 'CANDIDATE'
    assert len(runtime.state.uses['Q2']) == 1


def test_dormant_active_mislabel_is_downgraded_and_cannot_bind():
    runtime, manifest, _ = runtime_fixture()
    fid = ingest(runtime, manifest.entries[1], 'Q2', 'ACTIVE')
    assert not runtime.state.accepted_ids()
    assert runtime.state.uses['Q2'][fid].status == 'CANDIDATE'
    with pytest.raises(ValueError, match='ready inputs'):
        bind(runtime, 'Q2', 'Mary', fid)


def test_late_defer_without_new_binding_is_not_verified():
    runtime, manifest, _ = runtime_fixture()
    teacher = ingest(runtime, manifest.entries[2], 'Q1')
    bind(runtime, 'Q1', 'Alice', teacher)
    assert runtime.drain_activations() == ['Q2']
    runtime.finish_review('Q2')
    mother = ingest(runtime, manifest.entries[1], 'Q2', 'DORMANT')
    assert not runtime.drain_activations()
    with pytest.raises(ValueError, match='binding-triggered'):
        runtime.apply_reviews('Q2', {mother: 'MATCH'}, selected_ids={mother})
    assert mother not in runtime.state.accepted_ids()


def test_upstream_rebinding_invalidates_old_uses_and_links_without_deleting_facts():
    runtime, manifest, store = runtime_fixture()
    mother = ingest(runtime, manifest.entries[1], 'Q2', 'DORMANT')
    teacher = ingest(runtime, manifest.entries[2], 'Q1')
    bind(runtime, 'Q1', 'Alice', teacher, window=1)
    runtime.apply_reviews('Q2', {mother: 'MATCH'}, selected_ids={mother})
    runtime.finish_review('Q2')
    bind(runtime, 'Q2', 'Mary', mother, window=1)
    old_version = runtime.state.executions['Q2'].binding_version
    new = manifest.entries[2].model_copy(update={'source_ref': 'new', 'stream_position': 4, 'text': "Cindy's teacher is Bob.", 'text_sha256': None})
    # Re-validate after editing text so archive content integrity remains intact.
    from delaybind_core.manifest import ManifestEntry
    new = ManifestEntry.model_validate(new.model_dump())
    runtime.archive.append([new])
    bob = ingest(runtime, new, 'Q1', window=2)
    bind(runtime, 'Q1', 'Bob', bob, window=2)
    assert runtime.state.bindings == {'?teacher': 'Bob'}
    assert runtime.state.executions['Q2'].binding_version > old_version
    assert runtime.state.uses['Q2'][mother].status == 'INVALIDATED'
    assert mother in runtime.state.facts and teacher in runtime.state.facts
    assert not runtime.state.links
    assert mother in {fact.fact_id for fact in runtime.candidate_facts('Q2')}
    assert replay_events(store.list_runtime_events('unit')).export() == runtime.state.export()


def test_multi_fact_binding_link_keeps_joint_support_set():
    runtime, manifest, _ = runtime_fixture()
    first = ingest(runtime, manifest.entries[2], 'Q1')
    second = ingest(runtime, manifest.entries[0], 'Q1')
    runtime.apply_binding(BindingProposal(query_id='Q1', value='Alice', support_refs=[first, second]), window_index=0)
    runtime.finish_review('Q2')
    mother = ingest(runtime, manifest.entries[1], 'Q2')
    link = next(iter(runtime.state.links.values()))
    assert set(link.upstream_fact_ids) == {first, second}
    assert link.downstream_fact_id == mother


def test_full_collection_stays_open_until_scope_closes():
    plan = QueryPlan(plan_id='set', queries=[{'id': 'Q1', 'template': 'List all works by Alice.', 'output': '?works', 'requires_complete_set': True}])
    runtime, manifest, _ = runtime_fixture(plan)
    fid = ingest(runtime, manifest.entries[0], 'Q1')
    proposal = BindingProposal(query_id='Q1', value=['Book A'], support_refs=[fid])
    assert not runtime.apply_binding(proposal, window_index=0)
    assert runtime.state.executions['Q1'].status == 'ACTIVE'
    assert runtime.apply_binding(proposal, window_index=1, eof=True)


def test_plan_patch_versions_and_placeholder_boundaries():
    runtime, _, _ = runtime_fixture()
    runtime.patch_queries({'Q2': {'template': 'Who is ?teacher\'s father?'}}, window_index=1)
    assert runtime.state.executions['Q2'].version == 2
    assert runtime.state.query_projection('Q2')['template'] == "Who is ?teacher's father?"
    assert not runtime.drain_activations()
    bad = parse_plan(PLAN).model_dump()
    bad['queries'][0]['depends_on'] = ['Q3']
    with pytest.raises(PlanValidationError):
        ensure_valid_plan(QueryPlan.model_validate(bad))
    plan = QueryPlan(plan_id='vars', queries=[
        {'id': 'a', 'template': 'A?', 'output': '?person'},
        {'id': 'b', 'template': 'B?', 'output': '?person2'},
        {'id': 'c', 'template': '?person和?person2是否同岁？', 'output': '?answer', 'depends_on': ['a', 'b']},
    ])
    rt, _, _ = runtime_fixture(plan)
    rt._set_query('a', status='RESOLVED', result='甲')
    rt._set_query('b', status='RESOLVED', result='乙')
    assert rt.state.query_projection('c')['rendered_query'] == '甲和乙是否同岁？'


def test_parser_handles_fact_delimiters_negation_and_defer_word_without_misrouting():
    output = parse_update('Q1 | s1,s2 | Alice did not defer payment; A \\| B. | ACTIVE\nQ2 | s1 | Alice is not Mary\'s mother. | DEFER')
    assert output.facts[0].relevance == 'ACTIVE' and output.facts[0].source_refs == ['s1', 's2']
    assert 'A | B' in output.facts[0].text
    assert output.facts[1].text == "Alice is not Mary's mother."
    assert parse_update('[Q2 @ s1] Alice is Mary’s mother.（defer）').facts[0].relevance == 'DORMANT'
    assert parse_update('NONE').facts == []
    with pytest.raises(ProtocolError):
        parse_update('Q1 | s1 | fact | maybe')
    with pytest.raises(ProtocolError):
        parse_selection('SELECT | invented', {'F1'})
    with pytest.raises(ProtocolError):
        parse_judgments('F1 | MATCH', {'F1', 'F2'})
    assert parse_memory('BIND | Q1 | false | F1').bindings[0].value is False


def test_unread_sources_and_unsupported_binding_are_rejected():
    runtime, _, store = runtime_fixture()
    runtime.ingest(parse_update('Q1 | future | invented fact | ACTIVE'), window_index=0, allowed_refs={'future'})
    assert not runtime.state.facts
    with pytest.raises(ValueError, match='accepted fact'):
        bind(runtime, 'Q1', 'Alice', 'invented')
    assert any(event.event_type == 'FACT_SKIPPED' for event in store.list_runtime_events('unit'))


def test_query_can_finish_with_partial_or_empty_memory_without_sufficiency_gate():
    class EmptyClient:
        def __init__(self):
            self.calls = []
        async def complete(self, *, interface, **kwargs):
            self.calls.append(interface)
            if interface == 'PLAN':
                return PLAN
            if interface == 'UPDATE':
                return 'NONE'
            if interface == 'ANSWER':
                return r'\boxed{unknown}'
            raise AssertionError(interface)
    client = EmptyClient()
    result = asyncio.run(V5Runner(client).run(run_id='empty', question='A question', context='', store=SQLiteEventStore()))
    assert client.calls == ['PLAN', 'ANSWER']
    assert result['unresolved_queries'] == ['Q1', 'Q2', 'Q3']
    assert result['raw_answer'] == r'\boxed{unknown}' and result['state']['status'] == 'ANSWERED'


def test_distinct_high_and_low_clients_are_used():
    timeline = []
    high, low = ScriptedClient(role='HIGH', timeline=timeline), ScriptedClient(role='LOW', timeline=timeline)
    sample = canonicalize_record(record())
    result = asyncio.run(V5Runner(high, reader_client=low, config=RunnerConfig(chunk_size=1)).run(
        run_id='roles', sample=sample, store=SQLiteEventStore()))
    assert result['evidence_pack']['answer_value'] == 'Suzhou'
    assert result['agent_calls']['HIGH'] > 0 and result['agent_calls']['LOW'] > 0
    async def invoke(*args, **kwargs):
        return None
    with pytest.raises(ValueError):
        asyncio.run(HighLevelAgent(high, invoke).call('VERIFY', 'forbidden'))
    with pytest.raises(ValueError):
        asyncio.run(LowLevelAgent(low, invoke).call('MEMORY', 'forbidden'))


def test_rememr1_input_uses_token_chunks_and_needs_no_manifest():
    class Tokenizer:
        def encode(self, text): return list(text.encode('utf-32-le')[::4])
        def decode(self, tokens): return ''.join(chr(token) for token in tokens)
    class TextClient:
        def __init__(self): self.windows = []
        async def complete(self, *, interface, messages, **kwargs):
            if interface == 'PLAN': return 'Q1\nquery: What is stated?\noutput: ?answer\ndepends_on: NONE'
            if interface == 'UPDATE':
                self.windows.append(section(messages[0]['content'], 'Current window', 'Output only'))
                return 'NONE'
            if interface == 'ANSWER': return r'\boxed{unknown}'
            raise AssertionError(interface)
    client = TextClient()
    result = asyncio.run(V5Runner(client, tokenizer=Tokenizer(), config=RunnerConfig(chunk_size=2)).run(
        run_id='text', item={'_id': 0, 'input': 'What is stated?', 'context': 'abcd', 'answers': ['x']}, store=SQLiteEventStore()))
    assert result['windows_processed'] == 2 and result['window_budget_unit'] == 'tokens'
    assert '[0:c00000]' in client.windows[0] and 'ab' in client.windows[0]
    assert 'cd' not in client.windows[0] and 'cd' in client.windows[1]
    assert result['answer_format'] == 'boxed'
    assert canonicalize_record({'_id': 0, 'input': 'Q', 'context': 'C'}).sample_id == '0'


def test_archive_new_attempt_cannot_see_previous_attempt_future_prefix():
    store = SQLiteEventStore()
    first = RawArchive(store, 'attempt', session_prefix=True)
    cursor = TextReadCursor('abcdef', first, sample_id='x')
    cursor.next_window(3)
    cursor.next_window(3)
    second = RawArchive(store, 'attempt', session_prefix=True)
    restarted = TextReadCursor('abcdef', second, sample_id='x')
    restarted.next_window(3)
    assert not second.contains('x:c00001')
    assert len(second.fetch('x:c00000', neighborhood=10)) == 1


def test_plan_retries_and_memory_budget_failures_are_logged():
    result, _, client, _ = run_chain(client=ScriptedClient(plan_retry=True))
    assert client.plan_calls == 2 and result['evidence_pack']['answer_value'] == 'Suzhou'
    result, store, _, _ = run_chain(memory_char_budget=20)
    assert result['state']['status'] == 'RESOURCE_LIMIT'
    assert any(event.event_type == 'MEMORY_VIEW_MEASURED' for event in store.list_runtime_events('chain'))


def test_later_candidate_batch_conflict_is_not_ignored():
    class ConflictingClient(ScriptedClient):
        async def complete(self, *, interface, messages, **kwargs):
            if interface == 'VERIFY':
                prompt = messages[0]['content']
                candidates = facts_in(section(prompt, 'Selected deferred candidates', 'Their already-read original sources:'))
                return '\n'.join(f"{fid} | {'CONFLICT' if 'Jane' in text else 'MATCH'}" for fid, (_, text) in candidates.items())
            return await super().complete(interface=interface, messages=messages, **kwargs)
    raw = record()
    raw['context'].insert(2, ['Other', ["Alice's mother is Jane."]])
    sample = canonicalize_record(raw)
    result = asyncio.run(V5Runner(ConflictingClient(), config=RunnerConfig(chunk_size=1, candidate_batch_size=1)).run(
        run_id='conflict', sample=sample, store=SQLiteEventStore()))
    assert result['interface_calls']['VERIFY'] == 2
    assert '?mother' not in result['state']['bindings']
    assert result['state']['executions']['Q2']['conflicts']
    assert result['interface_calls']['ANSWER'] == 1


def test_cli_plain_input_and_saved_plan(tmp_path, monkeypatch):
    from delaybind_core import cli
    config = tmp_path / 'config.json'
    dataset = tmp_path / 'data.json'
    plan_file = tmp_path / 'plan.txt'
    output = tmp_path / 'result.json'
    dataset.write_text(json.dumps([record()]))
    plan_file.write_text(PLAN)
    config.write_text(json.dumps({'input': str(dataset), 'plan': str(plan_file), 'output': str(output),
                                  'base_url': 'https://unused.invalid', 'api_key': 'fixture', 'model': 'fixture', 'chunk_size': 1}))
    monkeypatch.setattr(cli, 'OpenAICompatibleClient', lambda *args, **kwargs: ScriptedClient())
    result = cli._run_from_config(config)
    assert json.loads(output.read_text()) == result
    assert result['evidence_pack']['answer_value'] == 'Suzhou'


def test_experiment_outputs_and_resume(tmp_path):
    from delaybind_core.evaluation import ExperimentConfig, run_experiment
    dataset = tmp_path / 'data.json'
    dataset.write_text(json.dumps([record()]))
    config = ExperimentConfig.from_mapping({'input': str(dataset), 'output_dir': str(tmp_path / 'results'),
             'methods': ['v5_predicted'], 'orders': ['original', 'reverse'], 'runner': {'chunk_size': 1}})
    first = asyncio.run(run_experiment(config, client_factory=lambda store: ScriptedClient()))
    assert first['count'] == 2
    assert all(group['answer_accuracy'] == 1 for group in first['groups'])
    assert asyncio.run(run_experiment(config, client_factory=lambda store: ScriptedClient())) == first


def test_intermediate_reasoning_without_candidates_uses_high_memory_not_verify():
    plan = QueryPlan(plan_id='choice', queries=[
        {'id': 'A', 'template': 'How old is Alice?', 'output': '?a'},
        {'id': 'B', 'template': 'How old is Bob?', 'output': '?b'},
        {'id': 'C', 'template': 'Who is younger given Alice is ?a and Bob is ?b?', 'output': '?person', 'depends_on': ['A', 'B']},
        {'id': 'D', 'template': 'Where was ?person born?', 'output': '?city', 'depends_on': ['C']},
    ])
    sample = canonicalize_record({'id': 'choice', 'question': 'Where was the younger person born?', 'context': [
        ['Birth', ['Alice was born in Paris.']], ['Alice', ['Alice is 20 years old.']], ['Bob', ['Bob is 40 years old.']],
    ]})
    class ChoiceClient:
        async def complete(self, *, interface, messages, **kwargs):
            prompt = messages[0]['content']
            if interface == 'UPDATE':
                sources = re.findall(r'\[([^\]]+)\][^\n]*\n(.*?)(?=\n\n\[|$)', section(prompt, 'Current window', 'Output only'), re.S)
                ref, text = sources[0]
                qid = 'D' if 'born' in text else 'A' if 'Alice' in text else 'B'
                return f"{qid} | {ref} | {text.strip()} | {'DORMANT' if qid == 'D' else 'ACTIVE'}"
            if interface == 'MEMORY':
                view = section(prompt, 'Plan', 'Working memory:')
                facts = facts_in(section(prompt, 'Working memory', 'Saved plan hints:'))
                for qid, needle, value in [('A', 'Alice is 20', 20), ('B', 'Bob is 40', 40), ('C', None, 'Alice'), ('D', 'born in Paris', 'Paris')]:
                    if f'{qid} [ACTIVE]' in view:
                        ids = [fid for fid, (_, text) in facts.items() if needle is None or needle in text]
                        if ids:
                            return f'BIND | {qid} | {json.dumps(value)} | {",".join(ids)}'
                return 'NONE'
            if interface == 'RECALL':
                assert 'D [ACTIVE]' in prompt
                return 'SELECT | ' + ','.join(facts_in(section(prompt, 'Deferred candidates', 'Return SELECT')))
            if interface == 'VERIFY':
                assert 'D [ACTIVE]' in prompt and 'C [ACTIVE]' not in prompt
                return '\n'.join(f'{fid} | MATCH' for fid in facts_in(section(prompt, 'Selected deferred candidates', 'Their already-read original sources:')))
            if interface == 'ANSWER':
                return json.dumps({'answer': 'Paris'})
            raise AssertionError(interface)
    result = asyncio.run(V5Runner(ChoiceClient(), config=RunnerConfig(chunk_size=1)).run(
        run_id='choice', sample=sample, plan=plan, store=SQLiteEventStore()))
    assert result['state']['bindings']['?person'] == 'Alice'
    assert result['state']['bindings']['?city'] == 'Paris'
    assert result['interface_calls']['VERIFY'] == 1
    assert len(result['evidence_pack']['facts']) == 3


@pytest.mark.parametrize('verdict', ['MISMATCH', 'UNCERTAIN', 'CONFLICT'])
def test_nonmatching_deferred_facts_are_never_promoted(verdict):
    runtime, manifest, _ = runtime_fixture()
    candidate = ingest(runtime, manifest.entries[1], 'Q2', 'DORMANT')
    root = ingest(runtime, manifest.entries[2], 'Q1')
    bind(runtime, 'Q1', 'Alice', root)
    assert not runtime.apply_reviews('Q2', {candidate: verdict}, selected_ids={candidate})
    assert candidate not in runtime.state.accepted_ids()
    assert runtime.state.executions['Q2'].status == 'ACTIVE'


def test_multi_source_fact_requires_all_referenced_sources():
    runtime, manifest, _ = runtime_fixture()
    refs = [manifest.entries[0].source_ref, manifest.entries[1].source_ref]
    event = FactEvent(query_id='Q1', text='A complete fact with a restored subject.', source_refs=refs, relevance='ACTIVE')
    runtime.ingest(FactUpdate(facts=[event]), window_index=0, allowed_refs={refs[0]})
    assert not runtime.state.facts
    runtime.ingest(FactUpdate(facts=[event]), window_index=0, allowed_refs=set(refs))
    assert next(iter(runtime.state.facts.values())).source_refs == sorted(refs)


def test_disabling_callbacks_preserves_defer_and_still_answers():
    result, _, _, _ = run_chain(enable_defer_callback=False)
    assert result['state']['defer_workspace']['Q2']
    assert result['state']['defer_workspace']['Q3']
    assert result['interface_calls'].get('VERIFY', 0) == 0
    assert result['interface_calls']['ANSWER'] == 1
    assert result['unresolved_queries'] == ['Q2', 'Q3']


def test_incomplete_streaming_protocol_is_diagnostic_and_does_not_gate_answer():
    result, _, _, _ = run_chain(chunk_size=1000, min_streaming_windows=2)
    assert not result['streaming_protocol_valid']
    assert result['interface_calls']['ANSWER'] == 1
    assert result['state']['status'] == 'ANSWERED'


def test_repeated_run_id_replay_uses_the_latest_attempt():
    sample = canonicalize_record(record())
    store = SQLiteEventStore()
    runner = V5Runner(ScriptedClient(), config=RunnerConfig(chunk_size=1))
    for _ in range(2):
        result = asyncio.run(runner.run(run_id='repeat', sample=sample, store=store))
    assert replay_events(store.list_runtime_events('repeat')).export() == result['state']


def test_agent_roles_are_preserved_in_api_bookkeeping(monkeypatch):
    from delaybind_core.api import APIConfig, OpenAICompatibleClient
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(APIConfig(base_url='https://unused.invalid', api_key='fixture', model='fixture'), store=store)
    monkeypatch.setattr(client, '_call_sync', lambda payload: ({'choices': [{'message': {'content': 'NONE'}}],
                                                              'usage': {'prompt_tokens': 12, 'completion_tokens': 1}}, 1))
    for role, interface in [('HIGH', 'MEMORY'), ('LOW', 'RECALL')]:
        assert asyncio.run(client.complete(run_id='roles', agent_role=role, interface=interface,
                                           messages=[{'role': 'user', 'content': 'test'}])) == 'NONE'
    calls = store.list_model_calls('roles')
    assert [(call.agent_role, call.interface) for call in calls] == [('HIGH', 'MEMORY'), ('LOW', 'RECALL')]
    assert set(store.model_call_summary('roles')['interface_usage']) == {'HIGH:MEMORY', 'LOW:RECALL'}


def test_memory_eval_adapter_preserves_rememr1_input_and_boxed_answer(monkeypatch):
    import sys
    import types
    from delaybind_core.api import OpenAICompatibleClient
    from taskutils.memory_eval.utils.v51 import async_query_llm
    settings = types.SimpleNamespace(URL='https://unused.invalid', API_KEY='fixture',
                                     RECURRENT_CHUNK_SIZE=2, RECURRENT_MAX_NEW=256)
    monkeypatch.setitem(sys.modules, 'taskutils.memory_eval.utils.envs', settings)
    calls = []
    async def fake_complete(self, *, interface, agent_role, **kwargs):
        calls.append((agent_role, interface))
        if interface == 'PLAN':
            return 'Q1\nquery: What is stated?\noutput: ?value\ndepends_on: NONE'
        if interface == 'UPDATE':
            return 'NONE'
        if interface == 'ANSWER':
            return r'\boxed{unknown}'
        raise AssertionError(interface)
    class Tokenizer:
        def encode(self, text): return [ord(char) for char in text]
        def decode(self, tokens): return ''.join(chr(token) for token in tokens)
    monkeypatch.setattr(OpenAICompatibleClient, 'complete', fake_complete)
    result = asyncio.run(async_query_llm({'_id': 0, 'input': 'What is stated?', 'context': 'abcd'},
                                        'fixture', Tokenizer()))
    assert result == r'\boxed{unknown}'
    assert calls == [('HIGH', 'PLAN'), ('LOW', 'UPDATE'), ('LOW', 'UPDATE'), ('HIGH', 'ANSWER')]


def test_unresolved_placeholder_cannot_be_bound_as_a_concrete_answer():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        BindingProposal(query_id='Q1', value='?teacher', support_refs=['F1'])
