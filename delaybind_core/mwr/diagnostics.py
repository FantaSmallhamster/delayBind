"""Explicit offline/oracle domain; never imported by inference engine."""
from .common import dump
from .answering import solve_answer, AnswerContract
from .scoring import score


def diagnostic_bundles(support_raw, retained_raw, bindings):
    def bundle(raw, relations=()):
        return {"raw_sources": raw, "supported_relations": list(relations),
                "unresolved_demands": [], "conflicts": []}
    return {
        "support_raw_oracle": bundle(support_raw),
        "same_raw_without_bindings": bundle(retained_raw),
        "same_raw_with_old_bindings": bundle(retained_raw, bindings),
        "window_extraction_oracle": bundle(support_raw),
        "simple_source_memory": bundle(retained_raw),
    }


async def run_answer_diagnostics(question, annotations, bundles, client, budget):
    """No oracle score is included in method aggregates. Extraction needs a separate reader replay."""
    rows = []
    for name, bundle in bundles.items():
        answer = await solve_answer(question, bundle, AnswerContract(), client, budget)
        rows.append({"diagnostic": name, "oracle": True, "answer": answer.model_dump(),
                     **score(answer.answer, annotations.answers),
                     "scope": "solver-only; not extraction/retention measurement"})
    return rows


async def diagnose_frozen_case(case, config, tokenizer, calls):
    """Four diagnostic families, using explicit offline annotation/candidate inputs.

    case: sample, predicted_plan, proposal_stream (per window EvidenceRecords),
    support_spans, optional legacy_bindings. Never called from normal inference.
    """
    from dataclasses import replace
    from ..data import CanonicalSample
    from .models import InferenceInput,EvalAnnotations,LiveState,Plan,EvidenceRecord,SourceSpan
    from .source_access import SourceAccess,span_id
    from .retention import retain
    from .packing import pack_answer
    from .common import digest,ProtocolError
    sample=CanonicalSample.model_validate(case['sample'])
    inp=InferenceInput.from_sample(sample);annotations=EvalAnnotations.from_sample(sample)
    plan=Plan.model_validate(case['predicted_plan'])
    supports=[SourceSpan.model_validate(s) for s in case['support_spans']]
    frozen=case['proposal_stream']
    def replay(policy,oracle=False):
        state=LiveState(plan=plan)
        access=SourceAccess(inp,tokenizer,neighbor_anchors=config.mwr.neighbor_anchors)
        for index,records in enumerate(frozen,1):
            rows=access.next_window(config.chunk_size)
            if rows is None: raise ProtocolError('proposal stream exceeds source windows')
            live_window=list(access._window_refs.values())
            if oracle:
                for target in supports:
                    for shown in live_window:
                        if target.doc_id!=shown.doc_id: continue
                        start,end=max(target.start,shown.start),min(target.end,shown.end)
                        if start>=end: continue
                        span=shown.model_copy(update={'start':start,'end':end})
                        cue=access._documents[span.doc_id][start:end]
                        eid='oracle-'+digest([span.model_dump(),cue])[:24]
                        state.evidence[eid]=EvidenceRecord(evidence_id=eid,source_spans=[span],cue_text=cue,observed_window=index)
            else:
                for raw in records:
                    e=EvidenceRecord.model_validate(raw)
                    if e.observed_window!=index: raise ProtocolError('proposal observation window mismatch')
                    for span in e.source_spans:
                        access._validate_span(span)
                        if not any(w.doc_id==span.doc_id and w.start<=span.start and span.end<=w.end for w in live_window):
                            raise ProtocolError('fixed proposal cites a different window')
                    state.evidence[e.evidence_id]=e
            state.version+=1
            state=retain(state,tokenizer,replace(config.mwr,retention_policy=policy),lambda *x:None)
            access.commit(state)
        if not access.exhausted: raise ProtocolError('proposal stream does not cover full forward input')
        return state,access
    states={name:replay(policy,oracle) for name,policy,oracle in [
        ('same_raw_without_bindings','demand',False),('window_extraction_oracle','demand',True),('simple_source_memory','fifo',False)]}
    bundles={}
    for name,(state,access) in states.items():
        bundles[name]=pack_answer(sample.question,state,access,calls,config,tokenizer,lambda *x:None)
    # Explicit offline support oracle. It may use gold pointers because this
    # result is labeled oracle and never aggregated as a method result.
    access=states['same_raw_without_bindings'][1]
    support_raw=[]
    for span in supports:
        access._validate_span(span)
        support_raw.append({'source_ref':span_id(span),'text':access._documents[span.doc_id][span.start:span.end]})
    bundles['support_raw_oracle']={'raw_sources':support_raw,'supported_relations':[], 'unresolved_demands':[],'conflicts':[]}
    unavailable=[]
    if 'legacy_bindings' in case:
        bundles['same_raw_with_old_bindings']={**bundles['same_raw_without_bindings'],'supported_relations':case['legacy_bindings']}
    else:
        unavailable.append({'diagnostic':'same_raw_with_old_bindings','reason':'legacy binding statements not supplied'})
    rows=[]
    for name,bundle in bundles.items():
        answer=await solve_answer(sample.question,bundle,AnswerContract(),calls,config.mwr)
        rows.append({'sample_id':sample.sample_id,'diagnostic':name,'diagnostic_only':True,
                     'answer':answer.model_dump(),**score(answer.answer,annotations.answers)})
    return {'rows':rows,'unavailable':unavailable,
        'disclosure':'One frozen predicted plan/proposal stream. Window oracle injects gold support only when first read; FIFO uses identical predicted candidates and capacity. These are diagnostics, not method scores.'}
