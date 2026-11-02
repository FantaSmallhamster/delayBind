import asyncio
import json
import pytest
from delaybind_core.data import CanonicalSample, build_manifest
from delaybind_core.direct import run_direct_mwr
from delaybind_core.mwr.answering import solve_answer, AnswerContract
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.common import ProtocolError, ResourceLimit
from delaybind_core.mwr.models import InferenceInput
from delaybind_core.mwr.source_access import SourceAccess
from delaybind_core.runner import RunnerConfig
from delaybind_core.storage import SQLiteEventStore
from tests.mwr.test_access_contract import CharacterTokenizer, EmptyClient


class CitationClient(EmptyClient):
    def __init__(self, always_invalid=False):
        super().__init__()
        self.always_invalid = always_invalid

    async def complete(self, **kw):
        self.calls.append(kw)
        ref = 'Document 1' if self.always_invalid or len(self.calls) == 1 else 's-valid'
        return json.dumps({'status': 'ANSWERED', 'answer': 'Elm', 'source_refs': [ref]})


def test_invalid_citation_gets_bounded_repair_without_new_sources():
    client = CitationClient(); config = RunnerConfig(engine='delaybind_mwr', max_response_retries=1)
    calls = CallBudget(client, None, CharacterTokenizer(), config, 'r', 'n', lambda *x: None)
    bundle = {'raw_sources': [{'source_ref': 's-valid', 'text': 'Neri was born in Elm.'}]}
    answer = asyncio.run(solve_answer('Where was Neri born?', bundle, AnswerContract(), calls, config.mwr))
    assert answer.source_refs == ['s-valid'] and calls.calls == 2 and calls.repairs == 1
    assert client.calls[1]['messages'][:2] == client.calls[0]['messages']
    assert 'copy source_ref values exactly' in client.calls[1]['messages'][-1]['content']


def test_repeated_invalid_citation_still_fails_within_budget():
    client = CitationClient(always_invalid=True); config = RunnerConfig(engine='delaybind_mwr', max_response_retries=1)
    calls = CallBudget(client, None, CharacterTokenizer(), config, 'r', 'n', lambda *x: None)
    with pytest.raises(ProtocolError):
        asyncio.run(solve_answer('q', {'raw_sources': [{'source_ref':'s-valid','text':'fact'}]}, AnswerContract(), calls, config.mwr))
    assert calls.calls == 2
    limited = CallBudget(CitationClient(), None, CharacterTokenizer(),
                         RunnerConfig(engine='delaybind_mwr', max_model_calls=1), 'r', 'n', lambda *x: None)
    with pytest.raises(ResourceLimit):
        asyncio.run(solve_answer('q', {'raw_sources': [{'source_ref':'s-valid','text':'fact'}]}, AnswerContract(), limited, config.mwr))
    assert limited.calls == 1


def test_raw_direct_uses_the_same_document_source_identity_as_memory():
    sample = CanonicalSample(sample_id='q', question='Where was Neri born?', answer='GOLD_CANARY',
        context='Document 1:\nNeri\nNeri was born in Elm.\n\nDocument 2:\nIvo\nIvo was born in Oak.\n')
    tok = CharacterTokenizer(); config = RunnerConfig(engine='delaybind_mwr', chunk_size=5000)
    access = SourceAccess(InferenceInput.from_sample(sample), tok)
    expected = []
    while not access.exhausted:
        expected.extend(access.next_window(config.chunk_size))
    client = EmptyClient()
    result = asyncio.run(run_direct_mwr(run_id='direct', question=sample.question,
        manifest=build_manifest(sample,order='original'), client=client, store=SQLiteEventStore(),
        config=config, tokenizer=tok, sample=sample))
    assert result['evidence_pack']['raw_sources'] == expected
    assert len(expected) == 2 and 'GOLD_CANARY' not in str(client.calls)
