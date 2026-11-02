"""Offline-only process metrics and paired question-cluster bootstrap.

This module is deliberately not imported by the runtime. Gold establishes t*,
not a method's binding time. Source reachability does not imply recall.
"""
import random
from collections import defaultdict
from dataclasses import dataclass,asdict


@dataclass(frozen=True)
class DelayedAnnotation:
    original_question_id: str
    query_id: str
    doc_id: str
    start: int
    end: int
    first_window: int
    bridge_complete_window: int
    expected_value: str | None = None
    mapping_status: str = 'EXACT'


def covers(span,target):
    return span.get('doc_id')==target.doc_id and span.get('start',-1)<=target.start and span.get('end',-1)>=target.end


def trace_metrics(events, annotations, answer_correct):
    records={}
    captures={}
    commits=[]
    recalls=[]
    assessments=[]
    packed=[]
    window=0
    for event in events:
        kind=event['event_type'];p=event['payload']
        if kind=='EVIDENCE_PROPOSED' and 'evidence' in p:
            e=p['evidence'];records[e['evidence_id']]=e
            captures.setdefault(e['evidence_id'],e['observed_window'])
        elif kind=='WINDOW_COMMITTED':
            window=p['window'];commits.append(p)
        elif kind=='RECALL_SELECTED': recalls.append((p.get('window',window+1),p))
        elif kind=='ASSESSMENT': assessments.append((window+1,p))
        elif kind=='ANSWER_PACKED': packed=p.get('visible_spans',[])
    rows=[]
    for annotation in annotations:
        row={'annotation':asdict(annotation)}
        if annotation.mapping_status!='EXACT':
            row.update({'included':False,'reason':'annotation mapping uncertain'});rows.append(row);continue
        if annotation.first_window>=annotation.bridge_complete_window:
            row.update({'included':False,'reason':'not delayed under the fixed input order'});rows.append(row);continue
        ids={eid for eid,e in records.items() if any(covers(s,annotation) for s in e['source_spans'])}
        captured=any(captures[eid]<=annotation.bridge_complete_window for eid in ids)
        # Survival before the bridge becomes usable; all methods retain this denominator.
        prior=[c for c in commits if c['window']<annotation.bridge_complete_window]
        at_star=prior[-1] if prior else {'evidence_ids':[],'reachable_spans':[]}
        survived=bool(ids & set(at_star.get('evidence_ids',[])))
        reachable=any(covers(s,annotation) for s in at_star.get('reachable_spans',[]))
        recalled=any(w>=annotation.bridge_complete_window and p.get('query_id')==annotation.query_id
                     and bool(ids&set(p.get('evidence_ids',[]))) for w,p in recalls)
        judged=[p for w,p in assessments if w>=annotation.bridge_complete_window and p.get('query_id')==annotation.query_id
                and bool(ids&set(p.get('support_ids',[])))]
        supported=any(p.get('status')=='SUPPORTED' and (annotation.expected_value is None or str(p.get('value'))==annotation.expected_value) for p in judged)
        final_covered=any(covers(s,annotation) for s in packed)
        failures=[]
        if not captured: failures.append('CAPTURE_FAILURE')
        if captured and not survived: failures.append('RECORD_RETENTION_FAILURE')
        if captured and not reachable: failures.append('SOURCE_UNREACHABLE')
        if survived and not recalled: failures.append('RECALL_FAILURE')
        if recalled and not supported: failures.append('SUPPORT_JUDGMENT_FAILURE')
        row.update({'included':True,'captured':captured,'cue_survived':survived,'source_reachable':reachable,
                    'recalled':recalled,'supported':supported,'final_covered':final_covered,'failure_labels':failures})
        rows.append(row)
    included=[r for r in rows if r.get('included')]
    complete=bool(included) and all(r['final_covered'] for r in included)
    # Coverage only supports a utilization diagnostic, not a causal attribution.
    if complete and not answer_correct:
        for r in included: r['failure_labels'].append('UTILIZATION_FAILURE_CANDIDATE')
    metrics={key:sum(r[key] for r in included)/len(included) if included else None
             for key in ('captured','cue_survived','source_reachable','recalled','supported','final_covered')}
    return {'denominator':len(included),'metrics':metrics,'rows':rows,'causal_claim':False}


def paired_bootstrap(rows, method_a, method_b, metric='answer_exact', *, repeats=2000,seed=4):
    grouped=defaultdict(lambda:defaultdict(dict))
    for row in rows:
        method=row['method']
        if method not in {method_a,method_b}: continue
        # Failures count as zero for accuracy; no dropped error rows.
        value=row.get(metric)
        if value is None and metric=='answer_exact': value=0
        if value is None: continue
        qid=row.get('original_question_id',row['sample_id'])
        variant=row.get('variant_id',row.get('order','original'))
        grouped[qid][method][variant]=float(value)
    differences=[];unpaired=[]
    for qid,methods in grouped.items():
        a,b=methods.get(method_a,{}),methods.get(method_b,{})
        if not a or set(a)!=set(b): unpaired.append(qid);continue
        differences.append(sum(a[v]-b[v] for v in a)/len(a))
    if not differences: return {'questions':0,'difference':None,'ci95':None,'unpaired_questions':unpaired}
    rng=random.Random(seed)
    draws=sorted(sum(rng.choices(differences,k=len(differences)))/len(differences) for _ in range(repeats))
    return {'questions':len(differences),'difference':sum(differences)/len(differences),
        'ci95':[draws[int(.025*(repeats-1))],draws[int(.975*(repeats-1))]],
        'unpaired_questions':unpaired,'resampling_unit':'original_question','repeats':repeats,'seed':seed}
