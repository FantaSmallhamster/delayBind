"""Regressions for contracts that the first real-model smoke did not satisfy."""
import asyncio
import pytest
from pydantic import ValidationError
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.models import Answer, Assessment, Plan
from delaybind_core.mwr.common import ResourceLimit
from delaybind_core.runner import RunnerConfig
from delaybind_core.mwr.config import MWRConfig
from .test_access_contract import CharacterTokenizer, EmptyClient


def test_plan_rejects_plain_output_name_before_runtime_validation():
    with pytest.raises(ValidationError):
        Plan.model_validate({'queries':[{'id':'q1','template':'Who directed Film A?', 'output':'director_name'}]})


@pytest.mark.parametrize('value',['Northport',1970,False,['A','B'],{'A':'Northport'}])
def test_explicit_result_types_preserve_supported_values(value):
    result=Assessment(input_fingerprint='fp',status='SUPPORTED',value=value,support_ids=['e1'],faithful=False)
    assert Assessment.model_validate_json(result.model_dump_json()).value == value
    assert Answer(status='ANSWERED',answer=value,source_refs=['s1']).answer == value


def test_schema_visible_in_actual_prompt_and_in_preflight_budget():
    client=EmptyClient();tok=CharacterTokenizer()
    cfg=RunnerConfig(engine='delaybind_mwr')
    calls=CallBudget(client,None,tok,cfg,'r','n',lambda *x:None)
    messages=[{'role':'user','content':'Return insufficient.'}]
    estimate=calls.check_request(messages,Answer,final=True)
    asyncio.run(calls.invoke('ANSWER',messages,Answer,final=True))
    request=client.calls[0]
    assert 'source_refs' in request['messages'][0]['content']
    assert 'Concise answer' in request['messages'][0]['content']
    assert estimate==calls._estimate(client,'ANSWER',request['messages'],request['response_schema'])
    limited=CallBudget(EmptyClient(),None,tok,RunnerConfig(engine='delaybind_mwr',
        mwr=MWRConfig(max_prompt_tokens=estimate+cfg.mwr.output_tokens-1)), 'r','n',lambda *x:None)
    with pytest.raises(ResourceLimit): limited.check_request(messages,Answer,final=True)
    with pytest.raises(ResourceLimit): asyncio.run(limited.invoke('ANSWER',messages,Answer,final=True))
    assert not limited.client.calls


def test_missing_support_is_repaired_before_returning_a_binding():
    class MissingSupport(EmptyClient):
        async def complete(self,**kw):
            self.calls.append(kw)
            return ('{"input_fingerprint":"fp","status":"SUPPORTED","value":"Elm"}' if len(self.calls)==1 else
                    '{"input_fingerprint":"fp","status":"SUPPORTED","value":"Elm","support_ids":["e1"]}')
    client=MissingSupport();cfg=RunnerConfig(engine='delaybind_mwr',max_response_retries=1)
    calls=CallBudget(client,None,CharacterTokenizer(),cfg,'r','n',lambda *x:None)
    result=asyncio.run(calls.invoke('MEMORY',[{'role':'user','content':'test'}],Assessment))
    assert result.support_ids==['e1'] and calls.repairs==1 and calls.calls==2
    schema=client.calls[0]['response_schema']
    assert set(schema['required'])==set(schema['properties'])


def test_invalid_plan_dependency_uses_bounded_repair():
    from delaybind_core.mwr.demands import validate_plan
    class BadPlan(EmptyClient):
        async def complete(self,**kw):
            self.calls.append(kw)
            return ('{"queries":[{"id":"q1","template":"Where was ?unknown born?","output":"?place"}]}'
                    if len(self.calls)==1 else '{"queries":[]}')
    client=BadPlan();cfg=RunnerConfig(engine='delaybind_mwr',max_response_retries=1)
    calls=CallBudget(client,None,CharacterTokenizer(),cfg,'r','n',lambda *x:None)
    result=asyncio.run(calls.invoke('PLAN',[{'role':'user','content':'test'}],Plan,validator=validate_plan))
    assert result.queries==[] and calls.repairs==1


@pytest.mark.parametrize('method,status',[('direct_mwr','INSUFFICIENT'),('delaybind_mwr','ERROR')])
def test_mwr_does_not_report_perfect_empty_legacy_graph_metrics(method,status):
    from delaybind_core.data import CanonicalSample
    from delaybind_core.metrics import score_result
    result=score_result({'schema_version':'delaybind-mwr-v1','method':method,'status':status,'state':{}},
        CanonicalSample(sample_id='q',question='q',answer='a',context='text'))
    assert result['graph_triple_f1'] is None and result['triple_event_f1'] is None
