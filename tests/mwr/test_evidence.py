from dataclasses import replace
from delaybind_core.mwr.models import Update, Plan, Demand
from delaybind_core.mwr.evidence import stage_proposals, migrate_legacy_update
from delaybind_core.mwr.config import MWRConfig
from .test_source_access import setup
from .test_access_contract import CharacterTokenizer


def test_m16_m21_unrouted_multiple_queries_and_wrong_routing():
    a,s=setup(); rows=a.next_window(100)
    s.plan=Plan(queries=[Demand(id='Q1',template='Who?',output='?who'),Demand(id='Q2',template='Where?',output='?where')])
    def ingest(qids, cue='a fact'):
        u=Update.model_validate({'evidence':[{'source_refs':[rows[0]['source_ref']],'cue_text':cue,'candidate_query_ids':qids}]})
        return stage_proposals(u,s,a,CharacterTokenizer(),MWRConfig(),lambda *x:None)
    un=ingest(['unknown']); assert len(un)==1
    e=next(iter(un.values())); assert not e.candidate_query_ids
    s.evidence.update(un)
    routed=ingest(['Q1','Q2']); assert set(routed)==set(un)
    assert next(iter(routed.values())).candidate_query_ids==['Q1','Q2']
    assert set(ingest([], 'different claim')) != set(un)
    s.assessments={'Q1':{'status':'INSUFFICIENT'}}
    assert e.evidence_id in s.evidence


def test_m18_plan_notes_cannot_become_evidence():
    update,audit=migrate_legacy_update([],['find birthplace',{'kind':'plan_note','text':'find father'},
        {'kind':'source_cue','source_refs':['s1'],'text':'B was born in X'}])
    assert update.plan_notes==['find father']
    assert len(update.evidence)==1 and not update.evidence[0].candidate_query_ids
    assert audit[0]['classification']=='UNCLASSIFIED'
