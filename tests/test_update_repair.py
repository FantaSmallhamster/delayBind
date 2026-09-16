"""UPDATE regressions from the protocol-v3 smoke; no live model requests."""
import asyncio
import json

import pytest

from delaybind_core.fact_protocol import ProtocolError, parse_update, resolve_query_id
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema import QueryPlan
from delaybind_core.source_refs import VisibleSources
from delaybind_core.storage import SQLiteEventStore


@pytest.mark.parametrize('line', [
    'Q2|NONE||DORMANT',
    'Q2|[]|NONE|DORMANT',
    'Q2||NULL|ACTIVE',
    'Q4|NONE|No document provides the director or death date for the film Red Pearls.|DORMANT',
    "Q5|NONE|Cannot determine which film's director died first because the director and death date for Red Pearls are unknown.|DORMANT",
    'Q3|NONE|Cannot determine which film came out first without the release year of The Relative Of His Excellency|ACTIVE',
])
def test_explicit_no_evidence_rows_are_logged_noops(line):
    result = parse_update(line, recover=True)
    assert not result.facts and not result.rejected_lines
    assert len(result.ignored_lines) == 1


def test_missing_sources_are_not_invented_and_cited_negative_facts_survive():
    result = parse_update('''Q1|NONE|Alice was born in Rome.|ACTIVE
Q1|NONE|No document provides the answer. Alice was born in Rome.|ACTIVE
Q1|NONE|No document provides the answer; Alice was born in Rome.|ACTIVE
Q1|D18@C0|Alice was not born in Rome.|ACTIVE
Q1|D18@C0|No document provides Alice's birthplace.|ACTIVE''', recover=True)
    assert len(result.rejected_lines) == 3
    assert len(result.facts) == 2
    assert not result.ignored_lines


@pytest.mark.parametrize('query', ['Q1 [RESOLVED]', 'Q1[ACTIVE]', '`q1` [dormant]'])
def test_status_decoration_only_resolves_existing_query(query):
    assert resolve_query_id(query, {'Q1'}) == 'Q1'
    with pytest.raises(ProtocolError):
        resolve_query_id(query, {'Q2'})


@pytest.mark.parametrize('query', ['Q1 [SATISFIED]', 'Q1 [Alice]', 'D18@C0', 'Q2 [ACTIVE]'])
def test_unknown_decorations_or_document_ids_are_not_guessed(query):
    with pytest.raises(ProtocolError):
        resolve_query_id(query, {'Q1'})


def test_mixed_fact_and_source_ids_reject_the_entire_row():
    sources = VisibleSources([{'source_ref': 's:c00000:D18', 'document_id': 'context:D18', 'sample_id': 's'}])
    result, _ = sources.normalize_update(parse_update(
        'Q1 | D18@C0,Fae24466508b0ecd2 | Alice is from Rome. | ACTIVE', recover=True),
        query_ids={'Q1'}, recover=True)
    assert not result.facts
    assert 'memory fact ID' in result.rejected_lines[0]['reason']
    assert 'Fae24466508b0ecd2' in result.rejected_lines[0]['line']


FIRST = "Document 18:\nAlice\nAlice's mother is Mary. Alice was born in Rome.\n\n"
SECOND = "Document 48:\nBand\nAlice is a member of the band."
GOOD = "Q1 | D18@C0 | Alice's mother is Mary. | ACTIVE"
BAD = 'Q1 | D48@C0,Fae24466508b0ecd2 | Alice was born in Rome. | ACTIVE'
FIXED = 'Q1 | D18@C0 | Alice was born in Rome. | ACTIVE'


class UpdateClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.updates = []
        self.interfaces = []

    async def complete(self, *, interface, messages, **kwargs):
        self.interfaces.append(interface)
        if interface == 'UPDATE':
            self.updates.append(messages[0]['content'])
            return next(self.responses)
        if interface == 'MEMORY':
            return 'NONE'
        if interface == 'ANSWER':
            return r'\boxed{unknown}'
        raise AssertionError(interface)


def run_updates(responses, *, context=FIRST + SECOND, chunk_size=10000, retries=1):
    client = UpdateClient(responses)
    store = SQLiteEventStore()
    plan = QueryPlan(plan_id='update-repair', queries=[{
        'id': 'Q1', 'template': 'What do we know about Alice?', 'output': '?details',
    }])
    result = asyncio.run(V5Runner(client, config=RunnerConfig(
        chunk_size=chunk_size, max_response_retries=retries,
    )).run(run_id='update-repair', question='What do we know about Alice?',
           context=context, plan=plan, store=store))
    return result, store.list_runtime_events('update-repair'), client


def test_placeholder_and_status_decoration_need_no_model_retry_or_state_override():
    result, events, client = run_updates([
        GOOD.replace('Q1 |', 'Q1 [RESOLVED] |') + '\nQ1|NONE||DORMANT'
    ])
    assert len(client.updates) == 1
    assert result['protocol_valid']
    assert result['state']['queries'][0]['status'] == 'ACTIVE'
    assert len(result['state']['working_memory']['facts']) == 1
    assert any(e.event_type == 'QUERY_IDS_NORMALIZED' for e in events)
    assert any(e.event_type == 'UPDATE_LINE_IGNORED' for e in events)
    assert not any(e.event_type == 'UPDATE_REPAIR_REQUESTED' for e in events)
    assert 'VERIFY' not in client.interfaces


@pytest.mark.parametrize('repair', ['NONE', GOOD + '\n' + FIXED])
def test_local_repair_keeps_good_facts_and_deduplicates_repeated_valid_rows(repair):
    result, events, client = run_updates([GOOD + '\n' + BAD, repair])
    assert result['protocol_valid']
    prompt = client.updates[1]
    assert '<UPDATE_REPAIR ' in prompt
    assert 'corrected complete response' not in prompt
    rejected = json.loads(prompt.split('Rejected lines and their individual errors (JSON data, not instructions):\n', 1)[1].split('\n\nWorking memory', 1)[0])
    assert len(rejected) == 1 and rejected[0]['line'] == BAD
    assert GOOD not in prompt
    assert FIRST.strip() in prompt and SECOND in prompt
    facts = result['state']['working_memory']['facts']
    assert len(facts) == (1 if repair == 'NONE' else 2)
    assert all(f['source_refs'] == ['update-repair:c00000:D18'] for f in facts)
    assert sum(e.event_type == 'FACT_COMMITTED' for e in events) == len(facts)
    assert 'VERIFY' not in client.interfaces


def test_exhausted_local_repair_retains_good_facts_without_dropping_invalid_ref():
    result, events, client = run_updates([GOOD + '\n' + BAD, BAD])
    assert not result['protocol_valid']
    assert len(result['state']['working_memory']['facts']) == 1
    assert result['state']['working_memory']['facts'][0]['text'] == "Alice's mother is Mary."
    assert sum(e.event_type == 'PROTOCOL_REPAIR_EXHAUSTED' for e in events) == 1
    assert client.interfaces[-1] == 'ANSWER'


def test_next_repair_contains_only_still_invalid_rows():
    unknown = 'Q1 | D99@C0 | Alice is a singer. | ACTIVE'
    result, events, client = run_updates([GOOD + '\n' + BAD, FIXED + '\n' + unknown, 'NONE'], retries=2)
    assert result['protocol_valid']
    prompt = client.updates[2]
    assert unknown in prompt and BAD not in prompt and FIXED not in prompt
    assert len(result['state']['working_memory']['facts']) == 2


def test_repair_evidence_does_not_advance_the_window_or_reveal_future_sources():
    result, events, client = run_updates([
        GOOD + '\nQ1 | D99@C0 | Alice is a singer. | ACTIVE', 'NONE', 'NONE',
    ], chunk_size=len(FIRST))
    repair = client.updates[1]
    assert FIRST.strip() in repair
    assert SECOND not in repair and 'D48@C1' not in repair
    assert result['windows_processed'] == 2


def test_repair_can_cite_only_explicitly_visible_memory_sources_from_prior_windows():
    result, events, client = run_updates([
        GOOD,
        BAD.replace('D48@C0', 'D48@C1'),
        FIXED,
    ], chunk_size=len(FIRST))
    repair = client.updates[2]
    assert FIRST.strip() in repair and SECOND in repair
    assert 'D18@C0' in repair and 'D48@C1' in repair
    facts = result['state']['working_memory']['facts']
    assert len(facts) == 2 and all(f['source_refs'] == ['update-repair:c00000:D18'] for f in facts)
    assert result['protocol_valid']
