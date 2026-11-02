"""Instance-scoped source-supported binding and dependency invalidation."""
from .models import Assessment, Binding
from .common import digest,dump,ProtocolError,ResourceLimit
from .demands import projection
from .recall import select_candidates
from .retention import invalidate,support_closure
from . import PROMPT_VERSION


def assessment_messages(payload):
    prompt='Make two independent judgments in one response. Faithful asks whether the candidate cues accurately represent their raw sources, regardless of relevance to this question. A true but irrelevant cue remains faithful. '
    prompt+='Status asks whether raw sources establish an answer to question_instance.rendered_query with its exact subject, relation, direction, negation, time and scope. '
    prompt+='Track the subject and object of each relation separately. A relation from A to B does not establish the same relation from B to A. A subject having a child does not identify that subject\'s parent; an outgoing relation cannot fill an unknown incoming relation. '
    prompt+='Derive the value from raw sources before checking the cues. You may connect supported relations across displayed sources, preserving entity identity and parent-path ownership. Do not use outside facts. '
    prompt+='A wrong cue does not make an otherwise answerable question insufficient: return SUPPORTED with the corrected raw-source value and faithful=false. Conversely a faithful cue need not answer the question. '
    prompt+='Explicit negation can establish No for a yes/no question. Mere absence of a relation cannot establish No or a reversed answer. Unresolved conflicting answers or a missing necessary link are INSUFFICIENT with value=null. '
    prompt+='Return input_fingerprint exactly, value, support_ids from candidate evidence IDs (not source_refs), faithful, qualifiers and coverage UNKNOWN/PARTIAL. '
    prompt+='For yes/no questions use the literal string "Yes" or "No", not a JSON boolean. A single observed value does not prove uniqueness or complete enumeration. Do not combine different subjects.\n'
    return [{'role':'user','content':prompt+dump(payload)}]


def apply_assessment(state, demand, assessment, fingerprint, selected_ids, parent_versions, log):
    if assessment.input_fingerprint!=fingerprint:
        raise ProtocolError('assessment input fingerprint mismatch')
    if not set(assessment.support_ids)<=set(selected_ids):
        raise ProtocolError('assessment supports IDs not presented')
    for dep,version in parent_versions.items():
        if dep not in state.bindings or state.bindings[dep].version!=version:
            raise ProtocolError('parent binding changed during assessment')
    qid=demand['id']
    state.assessments[qid]={'fingerprint':fingerprint,'status':assessment.status,
        'candidate_ids':selected_ids,'faithful':assessment.faithful,
        'qualifiers':assessment.qualifiers,'coverage':assessment.coverage}
    log('ASSESSMENT',{'query_id':qid,'input_fingerprint':fingerprint,**assessment.model_dump(),
                     'state_version':state.version})
    if assessment.status!='SUPPORTED':
        # Old support is not silently kept after counterevidence/uncertainty.
        invalidate(state,{qid},log,assessment.status)
        # Preserve this instance's negative result after clearing descendants.
        state.assessments[qid]={'fingerprint':fingerprint,'status':assessment.status,'candidate_ids':selected_ids}
        return
    if assessment.value is None or not assessment.support_ids:
        raise ProtocolError('supported binding requires value and evidence')
    # First version intentionally avoids cross-product inference over member sets.
    if isinstance(assessment.value,(list,dict)):
        state.assessments[qid]['status']='PARTIAL_UNSUPPORTED_COMBINATION'
        invalidate(state,{qid},log,'member combinations not implemented')
        state.assessments[qid]={'fingerprint':fingerprint,'status':'PARTIAL_UNSUPPORTED_COMBINATION','candidate_ids':selected_ids}
        log('COMBINATION_UNSUPPORTED',{'query_id':qid,'kept_in_evaluation':True})
        return
    previous=state.bindings.get(qid)
    same=previous and previous.value==assessment.value and previous.support_ids==assessment.support_ids and previous.parent_versions==parent_versions
    if previous and not same:
        # Invalidation clears old descendant judgments, but the just-computed
        # current-input judgment remains valid for this query. Dropping it would
        # immediately reissue the same assessment on the next event pass.
        current_assessment=state.assessments[qid]
        invalidate(state,{qid},log,'upstream value/support revised')
        state.assessments[qid]=current_assessment
    state.bindings[qid]=Binding(query_id=qid,input_fingerprint=fingerprint,value=assessment.value,
        support_ids=assessment.support_ids,parent_versions=parent_versions,
        version=previous.version if same else state.version+1,
        qualifiers=assessment.qualifiers,coverage='PARTIAL' if demand['requires_complete_set'] else assessment.coverage)
    state.version+=1
    log('BOUND',{'binding':state.bindings[qid].model_dump(),'state_version':state.version})
    log('DEMAND_CHANGED',{'query_id':qid,'state_version':state.version,'reason':'supported binding'})


async def process_events(state, access, calls, config, tokenizer, log, *, staged=None, eof=False):
    if config.engine!='delaybind_mwr': return
    settings=config.mwr
    events=0
    while events<settings.max_events_per_window:
        acted=False
        for query in state.plan.queries:
            # Earlier events in this pass may revise/invalidate an ancestor.
            # Refresh both rendered inputs and readiness before each query;
            # a pass-wide projection can retain a now-missing parent binding.
            demand=next(d for d in projection(state) if d['id']==query.id)
            if demand['status']=='DORMANT': continue
            if demand['depends_on'] and not settings.dependency_recall: continue
            selected,stats=select_candidates(demand,state.evidence,tokenizer,settings)
            if not selected: continue
            parent_versions={d:state.bindings[d].version for d in demand['depends_on']}
            parent_ids=set()
            for d in parent_versions:
                _,ids=support_closure(d,state); parent_ids.update(ids)
            ids=[e.evidence_id for e in selected]
            all_ids=sorted(set(ids)|parent_ids)
            access.authorize(state,staged)
            raw=access.preview_for_evidence(all_ids,state_version=state.version)
            # Exclude our own result/status/version: otherwise a successful bind
            # immediately changes its own cache key and triggers an endless loop.
            semantic_demand={k:v for k,v in demand.items() if k not in {'state_version','status'}}
            fp=digest({'plan_version':state.plan.version,'query':semantic_demand,'parents':parent_versions,
                'candidates':[e.model_dump() for e in selected], 'candidate_version':stats['candidate_version'],
                'visible_sources':raw,'prompt_version':PROMPT_VERSION,'model_contract':calls.namespace})
            if state.assessments.get(demand['id'],{}).get('fingerprint')==fp: continue
            raw_cost=sum(len(tokenizer.encode(r['text'])) for r in raw)
            remaining=settings.max_reread_tokens-access.lookup_tokens-settings.answer_source_tokens
            if raw_cost>min(settings.max_lookup_tokens,max(0,remaining)):
                log('BUDGET_BLOCKED',{'reason':'bind_source_budget','query_id':demand['id']})
                # Keep a bounded numeric/version judgment, not unsupported knowledge.
                state.assessments[demand['id']]={'fingerprint':fp,'status':'RESOURCE_LIMIT','candidate_ids':ids}
                continue
            log('RECALL_SELECTED',{'query_id':demand['id'],'evidence_ids':ids,'input_fingerprint':fp,
                                  'state_version':state.version,'window':access.windows,**stats})
            payload={'question_instance':semantic_demand,'input_fingerprint':fp,
                'upstream_support':{d:state.bindings[d].model_dump() for d in parent_versions},
                'candidates':[e.model_dump() for e in selected],'raw_sources':raw}
            messages=assessment_messages(payload)
            calls.check_request(messages,Assessment,final=False)
            access.lookup_for_evidence(all_ids,'BIND',min(settings.max_lookup_tokens,max(0,remaining)),state.version)
            assessment=await calls.invoke('MEMORY',messages,Assessment)
            apply_assessment(state,demand,assessment,fp,ids,parent_versions,log)
            events+=1; acted=True
            if events>=settings.max_events_per_window: break
        if not acted: break
    if events>=settings.max_events_per_window:
        log('EVENT_LIMIT',{'limit':events,'eof':eof,'reason':'pending changed inputs remain eligible'})
