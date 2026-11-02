import pytest
from delaybind_core.data import CanonicalSample, CanonicalDocument, build_manifest
from delaybind_core.mwr.models import InferenceInput, LiveState, EvidenceRecord, SourceSpan
from delaybind_core.mwr.source_access import SourceAccess, span_id
from delaybind_core.mwr.common import ProtocolError
from .test_access_contract import CharacterTokenizer


def record(access, row, eid='e1'):
    return EvidenceRecord(evidence_id=eid, source_spans=access.proposal_spans([row['source_ref']]),
        cue_text='cue', observed_window=access.windows)


def setup(text='abcdefghij', neighbor=0):
    access = SourceAccess(InferenceInput(sample_id='q', question='q', context=text),
                          CharacterTokenizer(), neighbor_anchors=neighbor)
    return access, LiveState()


def test_m10_m11_partial_anchor_and_guessed_refs():
    a,s = setup()
    rows=a.next_window(3)
    e=record(a,rows[0]); s.evidence[e.evidence_id]=e
    a.commit(s)
    assert a.lookup_for_evidence(['e1'],'BIND',100,s.version)[0]['text']=='abc'
    with pytest.raises(ProtocolError): a.lookup_for_evidence(['guessed'],'BIND',100,s.version)
    with pytest.raises(ProtocolError): a.proposal_spans([rows[0]['source_ref']])
    assert a.next_window(3)[0]['text']=='def'
    assert a.lookup_for_evidence(['e1'],'BIND',100,s.version)[0]['text']=='abc'


def test_m12_eviction_revokes_permission_but_other_record_can_cover():
    a,s=setup(); rows=a.next_window(10)
    e=record(a,rows[0]); e2=e.model_copy(update={'evidence_id':'e2','cue_text':'another claim'})
    s.evidence={'e1':e,'e2':e2}; a.commit(s)
    del s.evidence['e1']
    with pytest.raises(ProtocolError): a.lookup_for_evidence(['e1'],'ANSWER',100,s.version)
    assert a.lookup_for_evidence(['e2'],'ANSWER',100,s.version)
    del s.evidence['e2']
    with pytest.raises(ProtocolError): a.lookup_for_evidence(['e2'],'ANSWER',100,s.version)


def test_m13_source_stable_under_document_order_and_windows():
    docs=[CanonicalDocument(document_id='d1',title='A',sentences=['Alpha']),CanonicalDocument(document_id='d2',title='B',sentences=['Beta'])]
    inp=InferenceInput(sample_id='q',question='q',documents=docs)
    a=SourceAccess(inp,CharacterTokenizer()); b=SourceAccess(inp.model_copy(update={'documents':list(reversed(docs))}),CharacterTokenizer())
    ra=a.next_window(100); rb=b.next_window(100)
    assert {r['source_ref'] for r in ra}=={r['source_ref'] for r in rb}


def test_m14_fixed_neighbor_no_chaining_or_unread_leak():
    inp=InferenceInput(sample_id='q',question='q',documents=[CanonicalDocument(document_id='d',title='A',sentences=['a','b','c','d'])])
    a=SourceAccess(inp,CharacterTokenizer(),neighbor_anchors=1); s=LiveState()
    rows=a.next_window(3); e=record(a,rows[0]); s.evidence={'e1':e}; a.commit(s)
    visible=a.lookup_for_evidence(['e1'],'BIND',100,s.version)
    assert {r['text'] for r in visible}=={'a','b'}
    with pytest.raises(ProtocolError): a.proposal_spans([visible[-1]['source_ref']])
    a.next_window(10)
    assert {r['text'] for r in a.lookup_for_evidence(['e1'],'BIND',100,s.version)}=={'a','b'}


def test_m15_hash_and_state_version_enforced_and_cost_counted():
    a,s=setup(); rows=a.next_window(10); e=record(a,rows[0]); s.evidence={'e1':e}; a.commit(s)
    a.lookup_for_evidence(['e1'],'BIND',100,0); a.lookup_for_evidence(['e1'],'ANSWER',100,0)
    assert a.lookup_tokens==20
    with pytest.raises(ProtocolError): a.lookup_for_evidence(['e1'],'BIND',100,1)
    a._documents[e.source_spans[0].doc_id]='modified'
    with pytest.raises(ProtocolError): a.lookup_for_evidence(['e1'],'BIND',100,0)


def test_official_sentence_is_not_split_to_fill_previous_window():
    inp=InferenceInput(sample_id='q',question='q',documents=[CanonicalDocument(document_id='d',title='A',sentences=['12345','abcdef'])])
    access=SourceAccess(inp,CharacterTokenizer(),neighbor_anchors=0)
    assert [r['text'] for r in access.next_window(8)]==['12345']
    assert [r['text'] for r in access.next_window(8)]==['abcdef']
