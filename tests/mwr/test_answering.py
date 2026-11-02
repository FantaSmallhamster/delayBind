import asyncio
from dataclasses import replace
import pytest
from delaybind_core.mwr.answering import AnswerContract, solve_answer
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.models import InferenceInput
from delaybind_core.mwr.scoring import score
from delaybind_core.mwr.common import ProtocolError
from delaybind_core.data import CanonicalSample
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.direct import run_direct_mwr
from .test_access_contract import CharacterTokenizer, EmptyClient


def test_m05_same_bundle_same_actual_request():
    cfg, tok = RunnerConfig(engine='delaybind_mwr'), CharacterTokenizer()
    direct, memory = EmptyClient(), EmptyClient()
    bundle = {'raw_sources':[], 'supported_relations':[], 'unresolved_demands':[], 'conflicts':[]}
    asyncio.run(run_direct_mwr(run_id='direct', question='q', manifest=None, client=direct,
        store=SQLiteEventStore(), config=cfg, tokenizer=tok, evidence_bundle=bundle))
    budget = CallBudget(memory, None, tok, cfg, 'memory', 'different-engine-contract', lambda *x: None)
    asyncio.run(solve_answer('q', bundle, AnswerContract(), budget, cfg.mwr))
    assert direct.calls[0]['messages'] == memory.calls[0]['messages']
    assert direct.calls[0]['response_schema'] == memory.calls[0]['response_schema']
    assert direct.calls[0]['extra'] == memory.calls[0]['extra']


def test_m06_gold_canary_never_in_inference():
    sample = CanonicalSample(sample_id='q', question='q', context='ordinary source',
        answer='GOLD_CANARY', supporting_facts=[('GOLD_SUPPORT_CANARY',0)],
        metadata={'leak':'METADATA_CANARY'}, evidences=[['EVIDENCE_CANARY']])
    inp = InferenceInput.from_sample(sample)
    assert 'CANARY' not in inp.model_dump_json()
    client = EmptyClient()
    asyncio.run(V5Runner(client, config=RunnerConfig(engine='delaybind_mwr'), tokenizer=CharacterTokenizer()).run(
        run_id='gold', sample=sample, store=SQLiteEventStore()))
    assert 'CANARY' not in str(client.calls)


def test_m07_common_scoring_no_task_special_cases():
    assert not score('King Martin', ['Martin'])['answer_exact']
    assert score('The Martin.', ['martin'])['answer_exact']
    assert not score(None, [''])['answer_exact']


def test_m08_unshown_citation_rejected():
    class Bad(EmptyClient):
        async def complete(self, **kw):
            return '{"status":"ANSWERED","answer":"x","source_refs":["invented"]}'
    cfg = RunnerConfig(engine='delaybind_mwr')
    call = CallBudget(Bad(), None, CharacterTokenizer(), cfg, 'r', 'n', lambda *x: None)
    with pytest.raises(ProtocolError):
        asyncio.run(solve_answer('q', {'raw_sources':[]}, AnswerContract(), call, cfg.mwr))


def test_m09_four_diagnostic_families_on_frozen_stream():
    import json
    from delaybind_core.data import CanonicalDocument
    from delaybind_core.mwr.config import MWRConfig
    from delaybind_core.mwr.diagnostics import diagnose_frozen_case
    from .test_engine_flow import StoryClient
    sample=CanonicalSample(sample_id='diag',question='Where was the director of Film A born?',answer='Haizhou',
        documents=[CanonicalDocument(document_id='a',title='Chen',sentences=['Chen was born in Haizhou.']),
                   CanonicalDocument(document_id='b',title='Film A',sentences=['Film A was directed by Chen.'])])
    cfg=RunnerConfig(engine='delaybind_mwr',chunk_size=100,mwr=MWRConfig(max_live_state_tokens=6000,neighbor_anchors=0))
    store=SQLiteEventStore();tok=CharacterTokenizer()
    result=asyncio.run(V5Runner(StoryClient(),config=cfg,tokenizer=tok).run(run_id='build',sample=sample,store=store))
    proposals=[e.payload['evidence'] for e in store.list_runtime_events('build') if e.event_type=='EVIDENCE_PROPOSED']
    case={'sample':sample.model_dump(),'predicted_plan':result['state']['plan'],'proposal_stream':[proposals],
          'support_spans':[s for e in proposals for s in e['source_spans']],
          'legacy_bindings':[{'label':'synthetic incorrect binding','value':'Wrong Person'}]}
    call=CallBudget(StoryClient(),None,tok,cfg,'diag','diag',lambda *x:None)
    report=asyncio.run(diagnose_frozen_case(case,cfg,tok,call))
    assert len(report['rows'])==5 and not report['unavailable']
    assert all(r['diagnostic_only'] for r in report['rows'])
    assert all(r['answer_exact'] for r in report['rows'])
