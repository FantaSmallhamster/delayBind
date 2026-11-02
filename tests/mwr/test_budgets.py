import asyncio,json
from dataclasses import replace
import pytest
from delaybind_core.api import APIConfig,OpenAICompatibleClient
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.models import Update
from delaybind_core.mwr.common import ResourceLimit
from delaybind_core.runner import RunnerConfig,V5Runner
from delaybind_core.mwr.config import MWRConfig
from .test_access_contract import CharacterTokenizer,EmptyClient,guard
from .test_engine_flow import story


def test_m54_block_request_before_send():
    c=EmptyClient();cfg=RunnerConfig(engine='delaybind_mwr',mwr=MWRConfig(max_prompt_tokens=100))
    call=CallBudget(c,None,CharacterTokenizer(),cfg,'r','n',lambda *x:None)
    with pytest.raises(ResourceLimit): asyncio.run(call.invoke('UPDATE',[{'role':'user','content':'x'}],Update))
    assert not c.calls


def test_m55_m56_attempts_unknown_usage_and_repeated_cache(monkeypatch):
    store=SQLiteEventStore();c=OpenAICompatibleClient(APIConfig(base_url='http://unused',api_key='x',model='mock',max_retries=1),store=store)
    attempt=0
    def send(payload):
        nonlocal attempt
        attempt+=1
        if attempt==1: raise RuntimeError('network')
        return {'choices':[{'message':{'content':'{}'}}], 'usage':{'prompt_tokens':11,'completion_tokens':3}},1.
    monkeypatch.setattr(c,'_call_sync',send);monkeypatch.setattr(c,'_retry_delay',lambda *x:0)
    async def run():
        for _ in range(3): await c.complete(run_id='r',interface='UPDATE',messages=[{'role':'user','content':'x'}])
    asyncio.run(run());summary=store.model_call_summary('r')
    assert summary['physical_attempts']==2 and summary['failed_attempts']==1 and summary['cache_hits']==2
    assert summary['known_input_tokens']==11 and summary['input_tokens'] is None
    assert summary['usage_unknown_attempts']==1


def test_m57_only_live_ids_in_final_prompt(monkeypatch):
    guard(monkeypatch)
    result,c,store=story(limit=1600)
    packed=[e.payload for e in store.list_runtime_events('story') if e.event_type=='ANSWER_PACKED'][-1]
    assert set(packed['evidence_ids'])<=result['state']['evidence'].keys()
    assert result['live_state_tokens']<=1600


def test_m58_failed_calls_do_not_reset_budget_on_resume():
    class Fails(EmptyClient):
        async def complete(self,**kw):
            if kw['interface']=='UPDATE': raise ConnectionError('network')
            return await super().complete(**kw)
    c=Fails();s=SQLiteEventStore();cfg=RunnerConfig(engine='flat_memory_mwr',max_model_calls=3)
    runner=V5Runner(c,config=cfg,tokenizer=CharacterTokenizer())
    kw=dict(run_id='fail',question='q',context='text',store=s)
    for _ in range(2):
        with pytest.raises(ConnectionError): asyncio.run(runner.run(**kw))
    result=asyncio.run(runner.run(**kw))
    assert result['status']=='RESOURCE_LIMIT' and result['logical_model_calls']==3
