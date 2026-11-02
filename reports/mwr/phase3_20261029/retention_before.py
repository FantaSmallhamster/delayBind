"""Deterministic coverage retention with atomic proof-closure eviction."""
import re
from .common import dump, ResourceLimit
from .demands import projection


def tokens(state, tokenizer):
    return len(tokenizer.encode(dump(state.model_dump(exclude={"version", "status"}))))


def words(text):
    return set(re.findall(r'\w+', text.casefold()))


def bucket(record, demands):
    associated = [d for d in demands if d['id'] in record.candidate_query_ids]
    relation = words(' '.join(record.relation_cues))
    if not associated:
        associated = [d for d in demands if relation & words(' '.join(d['relation_cues']))]
    if any(d['status'] != 'DORMANT' for d in associated): return 'current'
    if associated: return 'dormant'
    return 'unrouted'


def support_closure(qid, state):
    if qid not in state.bindings: return set(), set()
    queries, evidence, pending = set(), set(), [qid]
    while pending:
        q = pending.pop()
        if q in queries: continue
        b = state.bindings.get(q)
        if b is None: return set(), set()
        queries.add(q); evidence.update(b.support_ids)
        for parent, version in b.parent_versions.items():
            if parent not in state.bindings or state.bindings[parent].version != version:
                return set(), set()
            pending.append(parent)
    if not evidence <= state.evidence.keys(): return set(), set()
    return queries, evidence


def invalidate(state, qids, log, reason):
    invalid=set(qids)
    while True:
        more={q for q,b in state.bindings.items() if set(b.parent_versions)&invalid}
        if more <= invalid: break
        invalid.update(more)
    for q in sorted(invalid):
        if q in state.bindings:
            removed=state.bindings.pop(q)
            log('INVALIDATED',{'query_id':q,'binding_version':removed.version,'reason':reason,'state_version':state.version})
        state.assessments.pop(q,None)
    return invalid


def retain(state, tokenizer, config, log):
    limit=config.max_live_state_tokens
    before=tokens(state,tokenizer)
    # All-fit invariant; still remove invalid proof objects.
    broken={q for q in state.bindings if not support_closure(q,state)[0]}
    invalidate(state,broken,log,'missing proof closure')
    if tokens(state,tokenizer)<=limit:
        for eid in state.evidence: log('EVIDENCE_RETAINED',{'evidence_id':eid,'reason':'all_fit','before_tokens':before,'after_tokens':tokens(state,tokenizer)})
        return state
    candidate=state.model_copy(deep=True)
    candidate.evidence={}; candidate.bindings={}; candidate.assessments={}; candidate.plan_notes=[]
    if tokens(candidate,tokenizer)>limit:
        raise ResourceLimit('plan alone exceeds live-state budget')
    # No unlimited pinned support. A closure is admitted only if it fits.
    for qid in sorted(state.bindings):
        qs,ids=support_closure(qid,state)
        trial=candidate.model_copy(deep=True)
        trial.bindings.update({q:state.bindings[q] for q in qs})
        trial.evidence.update({e:state.evidence[e] for e in ids})
        if tokens(trial,tokenizer)<=limit: candidate=trial
    demands=projection(state)
    pools={k:[] for k in ('current','dormant','unrouted')}
    for eid,e in state.evidence.items():
        if eid not in candidate.evidence: pools[bucket(e,demands)].append(e)
    for pool in pools.values(): pool.sort(key=lambda e:(-e.observed_window if config.retention_policy=='fifo' else e.observed_window,e.evidence_id))
    shares={'current':1-config.dormant_fraction-config.unrouted_fraction,
            'dormant':config.dormant_fraction,'unrouted':config.unrouted_fraction}
    if config.retention_policy=='current_only': shares={'current':1.,'dormant':0.,'unrouted':0.}
    if config.retention_policy=='uniform': shares={k:1/3 for k in pools}
    spare=limit-tokens(candidate,tokenizer)
    seen_subjects=set()
    def add(record, ceiling):
        trial=candidate.model_copy(deep=True); trial.evidence[record.evidence_id]=record
        if tokens(trial,tokenizer)<=ceiling:
            candidate.evidence[record.evidence_id]=record
            seen_subjects.update(record.entity_mentions)
            return True
        return False
    if config.retention_policy!='fifo':
        for kind in ('current','dormant','unrouted'):
            ceiling=min(limit,tokens(candidate,tokenizer)+int(spare*shares[kind]))
            # Subject diversity is a tie-break, never a semantic truth test.
            pool=list(pools[kind])
            while pool:
                pool.sort(key=lambda e:(bool(set(e.entity_mentions)&seen_subjects),e.observed_window,e.evidence_id))
                add(pool.pop(0),ceiling)
    remainder=[e for p in pools.values() for e in p if e.evidence_id not in candidate.evidence]
    if config.retention_policy=='fifo': remainder.sort(key=lambda e:(-e.observed_window,e.evidence_id))
    else:
        order={'current':0,'dormant':1,'unrouted':2}
        remainder.sort(key=lambda e:(order[bucket(e,demands)], bool(set(e.entity_mentions)&seen_subjects),e.observed_window,e.evidence_id))
    for e in remainder: add(e,limit)
    # Semantic caches/notes are optional and charged; never resurrect evicted IDs.
    for q,assessment in state.assessments.items():
        if set(assessment.get('candidate_ids',[]))<=candidate.evidence.keys():
            trial=candidate.model_copy(deep=True); trial.assessments[q]=assessment
            if tokens(trial,tokenizer)<=limit: candidate=trial
    for note in state.plan_notes:
        trial=candidate.model_copy(deep=True); trial.plan_notes.append(note)
        if tokens(trial,tokenizer)<=limit: candidate=trial
    removed=set(state.bindings)-set(candidate.bindings)
    invalidate(state,removed,log,'proof closure does not fit budget')
    # A descendant might have been collected through another closure; reconcile.
    for q in set(candidate.bindings)-set(state.bindings): candidate.bindings.pop(q)
    after=tokens(candidate,tokenizer)
    for eid in sorted(state.evidence):
        kept=eid in candidate.evidence
        log('EVIDENCE_RETAINED' if kept else 'EVIDENCE_EVICTED',
            {'evidence_id':eid,'reason':'budget_selection','before_tokens':before,'after_tokens':after,'state_version':state.version})
    assert after<=limit
    return candidate
