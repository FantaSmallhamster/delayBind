"""Complete support groups and live supplemental sources for the shared solver."""
from .answering import answer_request,AnswerContract
from .models import Answer
from .retention import support_closure,words
from .common import ResourceLimit


def pack_answer(question,state,access,calls,config,tokenizer,log):
    groups=[]
    covered=set()
    for qid in sorted(state.bindings):
        _,ids=support_closure(qid,state)
        if ids:
            groups.append(('binding:'+qid,ids)); covered.update(ids)
    query_words=words(question+' '+' '.join(q.template for q in state.plan.queries))
    extras=[e for eid,e in state.evidence.items() if eid not in covered]
    extras.sort(key=lambda e:(-len(query_words & words(e.cue_text+' '+' '.join(e.relation_cues))),e.observed_window,e.evidence_id))
    for e in extras: groups.append(('supplemental:'+e.evidence_id,{e.evidence_id}))
    selected=set()
    access.commit(state)
    def bundle(ids,raw):
        supported=[]
        if config.mwr.include_relation_hints:
            for qid,b in state.bindings.items():
                _,closure=support_closure(qid,state)
                if closure and closure<=ids:
                    supported.append({**b.model_dump(),'label':'SOURCE_SUPPORTED_NAVIGATION'})
        return {'raw_sources':raw,'supported_relations':supported,
            'unresolved_demands':[q.template for q in state.plan.queries if q.id not in state.bindings],
            'conflicts':[{'query_id':q,'status':a['status'],'qualifiers':a.get('qualifiers',[])}
                for q,a in state.assessments.items() if a.get('status')=='CONTRADICTED' and set(a.get('candidate_ids',[]))<=ids]}
    remaining=max(0,min(config.mwr.answer_source_tokens,config.mwr.max_reread_tokens-access.lookup_tokens))
    for label,group in groups:
        ids=selected|group
        raw=access.preview_for_evidence(sorted(ids),state_version=state.version)
        cost=sum(len(tokenizer.encode(r['text'])) for r in raw)
        try:
            if cost>remaining: raise ResourceLimit('answer sources')
            calls.check_request(answer_request(question,bundle(ids,raw),AnswerContract()),Answer,final=True)
        except ResourceLimit:
            log('ANSWER_GROUP_SKIPPED',{'group':label,'evidence_ids':sorted(group),'reason':'complete group exceeds budget'})
            continue
        selected=ids
    raw=access.preview_for_evidence(sorted(selected),state_version=state.version)
    result=bundle(selected,raw)
    calls.check_request(answer_request(question,result,AnswerContract()),Answer,final=True)
    access.lookup_for_evidence(sorted(selected),'ANSWER',remaining,state.version)
    log('ANSWER_PACKED',{'evidence_ids':sorted(selected),'source_refs':[r['source_ref'] for r in raw],
        'supplemental':[{'evidence_id':eid,'label':'SUPPLEMENTAL_NOT_BOUND'} for eid in sorted(selected-covered)],
        'state_version':state.version,'unresolved':len(result['unresolved_demands']),
        'visible_spans':[{k:v for k,v in r.items() if k!='text'} for r in raw]})
    return result
