from delaybind_core.mwr.analysis import DelayedAnnotation,trace_metrics,paired_bootstrap


def fixture(*,captured=True,survived=True,reachable=True,recalled=True,supported=True,final=True):
    span={'doc_id':'d','start':0,'end':5}
    def ev(k,**p): return {'event_type':k,'payload':p}
    events=[]
    if captured: events.append(ev('EVIDENCE_PROPOSED',evidence={'evidence_id':'e','observed_window':1,'source_spans':[span]}))
    events.append(ev('WINDOW_COMMITTED',window=1,evidence_ids=['e'] if survived else [],reachable_spans=[span] if reachable else []))
    if recalled: events.append(ev('RECALL_SELECTED',query_id='Q2',evidence_ids=['e']))
    if supported: events.append(ev('ASSESSMENT',query_id='Q2',support_ids=['e'],status='SUPPORTED',value='City'))
    events.append(ev('ANSWER_PACKED',visible_spans=[span] if final else []))
    return events


ANN=DelayedAnnotation('q','Q2','d',0,5,1,2,'City')


def test_m59_five_hand_checkable_failures():
    variants=[({'captured':False},'CAPTURE_FAILURE'),({'survived':False,'reachable':False},'RECORD_RETENTION_FAILURE'),
        ({'recalled':False},'RECALL_FAILURE'),({'supported':False},'SUPPORT_JUDGMENT_FAILURE'),({},'UTILIZATION_FAILURE_CANDIDATE')]
    for kw,label in variants:
        result=trace_metrics(fixture(**kw),[ANN],False)
        assert label in result['rows'][0]['failure_labels']


def test_m60_shared_source_reachable_after_cue_eviction():
    result=trace_metrics(fixture(survived=False,reachable=True),[ANN],True)
    assert result['metrics']['cue_survived']==0 and result['metrics']['source_reachable']==1


def test_m61_bridge_not_bound_stays_in_denominator_zero_is_na():
    assert trace_metrics(fixture(recalled=False,supported=False),[ANN],False)['denominator']==1
    assert trace_metrics([],[],False)['metrics']['captured'] is None


def test_m62_bootstrap_clusters_variants_and_counts_errors():
    rows=[{'sample_id':'q','method':m,'order':o,'answer_exact':v} for m,v in [('a',1),('b',0)] for o in ['original','reverse']]
    result=paired_bootstrap(rows,'a','b',repeats=100)
    assert result['questions']==1 and result['difference']==1 and result['ci95']==[1,1]
