from dataclasses import replace
import pytest
from delaybind_core.mwr.models import Plan, Demand, LiveState, Binding, EvidenceRecord, SourceSpan
from delaybind_core.mwr.demands import projection, validate_plan, reading_messages
from delaybind_core.mwr.retention import retain,tokens,invalidate
from delaybind_core.mwr.config import MWRConfig
from delaybind_core.mwr.common import ProtocolError,ResourceLimit
from .test_access_contract import CharacterTokenizer


def planned():
    return LiveState(plan=Plan(queries=[Demand(id='Q1',template='Who directed Film A?',output='?d',relation_cues=['director']),
        Demand(id='Q2',template='Where was ?d born?',output='?city',depends_on=['Q1'],relation_cues=['birthplace'])]))


def test_m22_m25_dormant_unknown_and_atomic_view():
    s=planned(); validate_plan(s.plan)
    assert projection(s)[1]['status']=='DORMANT'
    assert '?d' in projection(s)[1]['rendered_query']
    s.bindings['Q1']=Binding(query_id='Q1',input_fingerprint='i',value='Chen',support_ids=['e'],version=1)
    s.version+=1
    assert projection(s)[1]['rendered_query']=='Where was Chen born?'
    assert '?d' in s.plan.queries[1].template
    assert all(d['state_version']==s.version for d in projection(s))
    assert len(projection(planned(),include_dormant=False))==1


def test_invalid_plan_dependencies():
    s=planned(); s.plan.queries[0].depends_on=['Q2']
    with pytest.raises(ProtocolError): validate_plan(s.plan)


def rec(i, q='Q2'):
    return EvidenceRecord(evidence_id=str(i),source_spans=[SourceSpan(doc_id='d',start=i,end=i+1,source_hash='h')],
        cue_text=f'Person {i} was born in place {i}',entity_mentions=[f'Person {i}'],relation_cues=['birthplace'],
        candidate_query_ids=[q] if q else [],observed_window=i)


def test_m26_m28_determinism_capacity_and_all_fit():
    s=planned();s.evidence={str(i):rec(i) for i in range(20)}; tok=CharacterTokenizer()
    cfg=MWRConfig(max_live_state_tokens=2000)
    a=retain(s.model_copy(deep=True),tok,cfg,lambda *x:None)
    b=retain(s.model_copy(deep=True),tok,cfg,lambda *x:None)
    assert a==b and tokens(a,tok)<=2000 and len(a.evidence)<20
    full=retain(s,tok,replace(cfg,max_live_state_tokens=30000),lambda *x:None)
    assert full.evidence==s.evidence


def test_m30_m33_no_dangling_binding_or_semantic_cache():
    s=planned();s.evidence={'1':rec(1),'2':rec(2)}
    s.bindings={'Q1':Binding(query_id='Q1',input_fingerprint='x',value='parent',support_ids=['1'],version=1),
        'Q2':Binding(query_id='Q2',input_fingerprint='y',value='city',support_ids=['2'],parent_versions={'Q1':1},version=2)}
    s.assessments={'Q2':{'candidate_ids':['2'],'result':'city'}}
    tok=CharacterTokenizer(); limit=tokens(LiveState(plan=s.plan),tok)+50
    out=retain(s,tok,MWRConfig(max_live_state_tokens=limit),lambda *x:None)
    assert not out.bindings and not out.evidence and not out.assessments
    assert 'city' not in str(out.model_dump(exclude={'plan'}))


def test_plan_cannot_bypass_budget():
    s=planned();s.plan.queries[0].template='huge '*2000
    with pytest.raises(ResourceLimit): retain(s,CharacterTokenizer(),MWRConfig(),lambda *x:None)
