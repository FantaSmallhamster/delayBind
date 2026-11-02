import asyncio
import json
import pytest
from delaybind_core.mwr.binding import process_events
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.models import Plan, Demand, Binding, LiveState, EvidenceRecord, InferenceInput
from delaybind_core.mwr.source_access import SourceAccess
from delaybind_core.mwr.config import MWRConfig
from delaybind_core.runner import RunnerConfig
from .test_access_contract import CharacterTokenizer


@pytest.mark.parametrize('invalidated_query', ['q1', 'q2', 'revise'])
def test_later_queries_refresh_after_an_earlier_binding_is_invalidated(invalidated_query):
    tokenizer = CharacterTokenizer()
    plan = Plan(queries=[
        Demand(id='q1', template='Who is the parent?', output='?parent'),
        Demand(id='q2', template='Who is ?parent related to?', output='?child', depends_on=['q1']),
        Demand(id='q3', template='Where was ?child born?', output='?place', depends_on=['q2'])])
    access = SourceAccess(InferenceInput(sample_id='q', question='q', context='source facts'), tokenizer)
    access.next_window(100)
    spans = list(access._window_refs.values())
    evidence = EvidenceRecord(evidence_id='e1', source_spans=spans, cue_text='source facts',
                              candidate_query_ids=['q1','q2','q3'], observed_window=1)
    state = LiveState(plan=plan, version=2, evidence={'e1':evidence}, bindings={
        'q1': Binding(query_id='q1', input_fingerprint='old1', value='P', support_ids=['e1'], parent_versions={}, version=1),
        'q2': Binding(query_id='q2', input_fingerprint='old2', value='C', support_ids=['e1'], parent_versions={'q1':1}, version=1)})
    class Client:
        def __init__(self): self.queries=[]
        async def complete(self, **kw):
            payload=json.loads(kw['messages'][1]['content'].rsplit('\n',1)[1])
            qid=payload['question_instance']['id'];self.queries.append(qid)
            if invalidated_query=='revise':
                if qid=='q2': assert 'NewParent' in payload['question_instance']['rendered_query']
                if qid=='q3': assert 'NewChild' in payload['question_instance']['rendered_query']
            else:
                assert qid!='q3', 'a child with an invalidated parent must not reach the model'
            rejected=qid==invalidated_query
            value={'q1':'NewParent','q2':'NewChild','q3':'Elm'}[qid] if invalidated_query=='revise' else 'P'
            return json.dumps({'input_fingerprint':payload['input_fingerprint'],
                'status':'INSUFFICIENT' if rejected else 'SUPPORTED','value':None if rejected else value,
                'support_ids':[] if rejected else ['e1']})
    client=Client();config=RunnerConfig(engine='delaybind_mwr',mwr=MWRConfig(max_prompt_tokens=32000))
    calls=CallBudget(client,None,tokenizer,config,'r','n',lambda *x:None)
    asyncio.run(process_events(state,access,calls,config,tokenizer,lambda *x:None))
    if invalidated_query=='revise':
        assert state.bindings['q3'].value=='Elm'
        assert client.queries==['q1','q2','q3']
    else:
        assert invalidated_query not in state.bindings and 'q3' not in state.bindings
        assert client.queries==(['q1'] if invalidated_query=='q1' else ['q1','q2'])
