import asyncio,json
from dataclasses import replace
import pytest
from delaybind_core.data import CanonicalSample,CanonicalDocument
from delaybind_core.runner import RunnerConfig,V5Runner
from delaybind_core.mwr.config import MWRConfig
from delaybind_core.mwr.models import LiveState,Assessment,Binding
from delaybind_core.mwr.binding import apply_assessment
from delaybind_core.mwr.demands import projection
from delaybind_core.mwr.recall import select_candidates
from delaybind_core.storage import SQLiteEventStore
from .test_access_contract import CharacterTokenizer,guard
from .test_demands_retention import planned,rec


class StoryClient:
    def __init__(self): self.calls=[]
    async def complete(self,**kw):
        self.calls.append(kw)
        interface=kw['interface']
        content=kw['messages'][1]['content']
        if interface=='PLAN': return planned().plan.model_dump_json()
        body=json.loads(content.rsplit('\n',1)[1])
        if interface=='UPDATE':
            items=[]
            for row in body['window']:
                text=row['text']
                if 'Chen was born in Haizhou.' in text:
                    items.append({'source_refs':[row['source_ref']],'cue_text':'Chen birthplace Haizhou',
                                  'entity_mentions':['Chen'],'relation_cues':['birthplace'],'candidate_query_ids':[]})
                if 'Film A was directed by Chen.' in text:
                    items.append({'source_refs':[row['source_ref']],'cue_text':'Film A director Chen',
                                  'entity_mentions':['Film A','Chen'],'relation_cues':['director'],'candidate_query_ids':['Q1']})
            return json.dumps({'evidence':items})
        if interface=='MEMORY':
            query=body['question_instance']; candidates=body['candidates']
            wanted='director' if query['id']=='Q1' else 'birthplace'
            support=[e['evidence_id'] for e in candidates if wanted in e['relation_cues']]
            raw=' '.join(r['text'] for r in body['raw_sources'])
            valid=bool(support) and (('directed by Chen' in raw) if wanted=='director' else ('Chen was born in Haizhou' in raw))
            return json.dumps({'input_fingerprint':body['input_fingerprint'],'status':'SUPPORTED' if valid else 'INSUFFICIENT',
                'value':('Chen' if wanted=='director' else 'Haizhou') if valid else None,'support_ids':support if valid else []})
        if interface=='ANSWER':
            raw=body['evidence_bundle']['raw_sources']; text=' '.join(r['text'] for r in raw)
            sufficient='directed by Chen' in text and 'born in Haizhou' in text
            return json.dumps({'status':'ANSWERED' if sufficient else 'INSUFFICIENT','answer':'Haizhou' if sufficient else None,
                              'source_refs':[r['source_ref'] for r in raw] if sufficient else []})
        raise AssertionError(interface)


def story(*,same_window=False,engine='delaybind_mwr',limit=6000):
    # Exact 100-character documents make independent windows deterministic.
    a='Chen was born in Haizhou.'; b='Film A was directed by Chen.'
    sentences=[a+' '*(100-len(a)), 'irrelevant '*9+' ', b+' '*(100-len(b))]
    sample=CanonicalSample(sample_id='story',question='Where was the director of Film A born?',
        documents=[CanonicalDocument(document_id=str(i),title=str(i),sentences=[text]) for i,text in enumerate(sentences)])
    cfg=RunnerConfig(engine=engine,chunk_size=400 if same_window else 100,
        mwr=MWRConfig(max_live_state_tokens=limit,max_candidate_tokens=5000,max_staging_tokens=5000,neighbor_anchors=0))
    c,s=StoryClient(),SQLiteEventStore()
    result=asyncio.run(V5Runner(c,config=cfg,tokenizer=CharacterTokenizer()).run(run_id='story',sample=sample,store=s))
    return result,c,s


@pytest.mark.parametrize('same_window',[False,True])
def test_m16_m34_m35_bridge_and_property_before_eof(monkeypatch,same_window):
    guard(monkeypatch)
    result,client,store=story(same_window=same_window)
    assert result['answer']['answer']=='Haizhou'
    assert result['state']['bindings']['Q2']['value']=='Haizhou'
    interfaces=[c['interface'] for c in client.calls]
    assert interfaces[-1]=='ANSWER' and 'MEMORY' in interfaces[:-1]
    commits=[e for e in store.list_runtime_events('story') if e.event_type=='WINDOW_COMMITTED']
    assert all(e.payload['staging_tokens']==0 and e.payload['live_state_tokens']<=6000 for e in commits)
    assert result['windows_processed']==(1 if same_window else 3)
    assert result['live_state_tokens']<=6000


def test_m36_m37_no_unchanged_reviews_no_eof_read():
    result,client,store=story()
    reviewed=[json.loads(c['messages'][1]['content'].rsplit('\n',1)[1])['input_fingerprint'] for c in client.calls if c['interface']=='MEMORY']
    assert len(reviewed)==len(set(reviewed))
    assert len([c for c in client.calls if c['interface']=='UPDATE'])==3


def test_m38_m42_recall_is_live_bounded_and_not_support():
    s=planned();s.evidence={str(i):rec(i) for i in range(20)}
    selected,stats=select_candidates(projection(s)[1],s.evidence,CharacterTokenizer(),MWRConfig(max_candidates=2))
    assert len(selected)<=2 and stats['unreviewed']>=18
    assert not s.bindings
    s.evidence={}
    assert not select_candidates(projection(s)[1],s.evidence,CharacterTokenizer(),MWRConfig())[0]


def test_m43_m48_instance_validation_and_descendant_invalidation():
    s=planned();s.evidence={'1':rec(1),'2':rec(2)}
    d=projection(s)[0]
    valid=Assessment(input_fingerprint='fp',status='SUPPORTED',value='Chen',support_ids=['1'])
    apply_assessment(s,d,valid,'fp',['1'],{},lambda *x:None)
    s.bindings['Q2']=Binding(query_id='Q2',input_fingerprint='fp2',value='Haizhou',support_ids=['2'],version=2,parent_versions={'Q1':s.bindings['Q1'].version})
    apply_assessment(s,d,valid.model_copy(update={'value':'Other'}),'fp',['1'],{},lambda *x:None)
    assert 'Q2' not in s.bindings and s.bindings['Q1'].value=='Other'
    with pytest.raises(ValueError): apply_assessment(s,d,valid,'wrong',['1'],{},lambda *x:None)
    with pytest.raises(ValueError): apply_assessment(s,d,valid,'fp',['2'],{},lambda *x:None)


def test_m49_m53_unbound_sources_and_eviction_final_pack(monkeypatch):
    guard(monkeypatch)
    flat,_,_=story(engine='flat_memory_mwr')
    assert flat['answer']['answer']=='Haizhou' and not flat['state']['bindings']
    limited,_,store=story(limit=700)
    assert limited['status'] in {'INSUFFICIENT','RESOURCE_LIMIT'}
    assert not limited['evidence_pack']['raw_sources'] or limited['answer']['answer'] is None
