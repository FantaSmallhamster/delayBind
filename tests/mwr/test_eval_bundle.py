import hashlib,json,zipfile
import pytest
from scripts.mwr.bundle_tools import import_archive,audit_bundle,grade_rows,bundle_score,documents
from scripts.mwr.map_support_sentences import align_sentences,map_bundle
from delaybind_core.data import CanonicalSample,canonicalize_record
from delaybind_core.metrics import score_result,summarize_results
from delaybind_core.mwr.models import InferenceInput
from delaybind_core.mwr.source_access import SourceAccess
from .test_access_contract import CharacterTokenizer


def fixture_bundle(tmp_path):
    row={'id':'new','input':'Where was Neri born?','type':'compositional','num_docs':1,
         'context':'Document 1:\nNeri\nNeri was born in Elm.\n\n'}
    gold={'sample_id':'new','official_id':'official-new','question':row['input'],'question_type':row['type'],
        'gold_answers':['GOLD_CANARY'],'evidence_idx':[0,0],'supporting_facts':[['Neri',0]],
        'evidences':[['Neri','birthplace','Elm']],'evidences_id':[]}
    files={'test/eval_2wikimultihopqa_50.json':json.dumps([row]).encode(),
        'grading/gold_answers.jsonl':(json.dumps(gold)+'\n').encode(),
        'metadata/selection_summary.json':b'{}','grading/score_results.py':b'raise AssertionError("never execute")'}
    sums=''.join(hashlib.sha256(b).hexdigest()+'  '+n+'\n' for n,b in files.items())
    archive=tmp_path/'bundle.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for n,b in files.items():z.writestr('bundle/'+n,b)
        z.writestr('bundle/metadata/SHA256SUMS.txt',sums)
    old=tmp_path/'old.json';old.write_text('[]')
    return archive,row,gold,old


def test_import_audit_and_source_identity_without_gold_leak(tmp_path):
    archive,row,gold,old=fixture_bundle(tmp_path);destination=tmp_path/'imported'
    imported=import_archive(archive,destination)
    assert len(imported['verified_files'])==4
    assert import_archive(archive,destination)==imported
    report,maps=audit_bundle(destination,old)
    assert report['sample_count']==1 and report['support_document_count']==1
    assert report['samples_without_optional_evidence_entity_ids']==1
    assert maps[0]['support_documents'][0]['sentence_mapping_status']=='UNRESOLVED_NO_SENTENCE_ARRAY'
    inp=InferenceInput.from_sample(canonicalize_record(row));assert 'CANARY' not in inp.model_dump_json()
    access=SourceAccess(inp,CharacterTokenizer())
    assert maps[0]['support_documents'][0]['doc_id'] in access._documents
    assert not any(report['overlap_with_development'].values())


def test_archive_corruption_is_rejected_before_extraction(tmp_path):
    archive,row,gold,old=fixture_bundle(tmp_path)
    with zipfile.ZipFile(archive) as z:files={n:z.read(n) for n in z.namelist()}
    files['bundle/test/eval_2wikimultihopqa_50.json']=b'changed'
    with zipfile.ZipFile(archive,'w') as z:
        for n,b in files.items():z.writestr(n,b)
    destination=tmp_path/'imported'
    with pytest.raises(ValueError,match='checksum'):import_archive(archive,destination)
    assert not destination.exists()


def test_blind_harness_accuracy_is_unknown_until_offline_join():
    sample=CanonicalSample(sample_id='q',question='q',context='text')
    row=score_result({'schema_version':'delaybind-mwr-v1','method':'direct_mwr','order':'original','status':'ANSWERED',
        'answer':{'status':'ANSWERED','answer':'Elm','source_refs':['s1']}},sample)
    assert row['answer_exact'] is None and row['grading_status']=='PENDING_EXTERNAL_GOLD'
    assert summarize_results([row])['groups'][0]['answer_accuracy'] is None


def test_offline_join_keeps_full_denominator_and_two_metric_contracts():
    gold=[{'sample_id':'a','gold_answers':['Elm']},{'sample_id':'b','gold_answers':['yes']}]
    rows=[{'sample_id':'a','method':'m','order':'original','status':'ANSWERED','prediction':'The Elm'}]
    report=grade_rows(rows,gold,['m','missing_method'],['original']);g=report['groups'][0]
    assert g['count']==2 and g['missing']==1 and g['primary_mwr_em']==.5 and g['bundle_em']==0
    assert report['groups'][1]['missing']==2
    rows[0]['status']='ERROR';rows[0]['prediction']='Elm'
    assert grade_rows(rows,gold,['m'],['original'])['groups'][0]['primary_mwr_em']==0
    with pytest.raises(ValueError,match='duplicate'):grade_rows(rows+rows,gold,['m'],['original'])
    with pytest.raises(ValueError,match='unknown'):grade_rows(rows,gold,['other'],['original'])


def test_bundle_boolean_and_unicode_normalization():
    assert bundle_score(False,['No'])['exact']
    assert bundle_score('TRUE',['yes'])['exact']
    assert bundle_score('Москва!',['москва'])['exact']
    assert not bundle_score('The Elm',['Elm'])['exact']


def test_document_numbering_cannot_silently_shift_support_indices():
    with pytest.raises(ValueError,match='non-contiguous'):
        documents('Document 1:\nOne\nx\nDocument 3:\nThree\ny')


def test_sentence_alignment_preserves_unicode_offsets_and_repeated_occurrences():
    sentences=[' Éva was born in 東京. ', ' Éva was born in 東京. ', 'Her mother was Lea.']
    body='Éva\nÉva  was born in 東京.Éva was born in 東京. Her\tmother was Lea.\n\n'
    rows=align_sentences('Éva',sentences,body)
    assert all(r['mapping_status']=='WHITESPACE_ALIGNED' for r in rows)
    assert rows[0]['end']<=rows[1]['start']<rows[1]['end']<=rows[2]['start']
    assert body[rows[1]['start']:rows[1]['end']]=='Éva was born in 東京.'
    assert body[rows[2]['start']:rows[2]['end']]=='Her\tmother was Lea.'
    exact=align_sentences('Éva',sentences,'Éva\n'+''.join(sentences)+'\n\n')
    assert all(r['mapping_status']=='EXACT' for r in exact)
    assert all(r['sentence_text_equal'] for r in exact)


@pytest.mark.parametrize('changed',['Alice was not born in Elm.','Alice was born in Oak.','Alice was born in Elm!'])
def test_sentence_alignment_refuses_non_whitespace_edits_even_outside_target(changed):
    rows=align_sentences('Alice',['Alice was born in Elm.','She met Neri.'],
                         'Alice\n'+changed+'She met Neri.')
    assert all(r['mapping_status']=='UNCERTAIN' and 'start' not in r for r in rows)


def test_official_mapping_validates_source_and_never_exports_guessed_t_star(tmp_path):
    archive,row,gold,old=fixture_bundle(tmp_path);destination=tmp_path/'imported'
    import_archive(archive,destination);_,maps=audit_bundle(destination,old)
    official=tmp_path/'selected_official.json'
    o={'_id':gold['official_id'],'question':row['input']+' ',
       'supporting_facts':gold['supporting_facts'],
       'context':[['Neri',['Neri was born in Elm.']],['Neri',['Neri was born in Elm.']]]}
    official.write_text(json.dumps([o]))
    report,facts=map_bundle(destination,official,maps)
    assert report['question_edge_whitespace_only']==1
    assert report['identical_official_document_duplicates']==1
    assert report['support_fact_mapping_counts']=={'EXACT':1}
    fact=facts[0]
    assert row['context'][fact['context_start']:fact['context_end']]==fact['text']=='Neri was born in Elm.'
    assert 'bridge_complete_window' not in fact and 'query_id' not in fact
    assert report['t_star_status'].startswith('PENDING_')
    assert maps[0]['support_documents'][0]['sentence_mapping_status']=='EXACT'
    o['context'][1][1]=['Neri was born in Oak.'];official.write_text(json.dumps([o]))
    with pytest.raises(ValueError,match='ambiguous repeated'):map_bundle(destination,official,maps)
    o['context']=o['context'][:1]
    # Unrelated question edits must not inherit a previous exact span assertion.
    o['question']='Who is Neri?';official.write_text(json.dumps([o]))
    with pytest.raises(ValueError,match='question differs'):map_bundle(destination,official,maps)
    o['question']=row['input'];o['supporting_facts']=[['Neri',5]]
    official.write_text(json.dumps([o]))
    with pytest.raises(ValueError,match='supporting facts differ'):map_bundle(destination,official,maps)


def test_official_mapping_rejects_stale_document_identity(tmp_path):
    archive,row,gold,old=fixture_bundle(tmp_path);destination=tmp_path/'imported'
    import_archive(archive,destination);_,maps=audit_bundle(destination,old)
    official=tmp_path/'selected_official.json'
    official.write_text(json.dumps([{'_id':gold['official_id'],'question':row['input'],
        'supporting_facts':gold['supporting_facts'],'context':[['Neri',['Neri was born in Elm.']]]}]))
    maps[0]['support_documents'][0]['source_hash']='stale'
    with pytest.raises(ValueError,match='stale document map'):map_bundle(destination,official,maps)


def test_resegmented_arrays_cannot_silently_shift_official_support_index(tmp_path):
    archive,row,gold,old=fixture_bundle(tmp_path);destination=tmp_path/'imported'
    import_archive(archive,destination)
    gold['supporting_facts']=[['Neri',1]]
    (destination/'grading/gold_answers.jsonl').write_text(json.dumps(gold)+'\n')
    _,maps=audit_bundle(destination,old)
    official=tmp_path/'selected_official.json'
    official.write_text(json.dumps([{'_id':gold['official_id'],'question':row['input'],
        'supporting_facts':gold['supporting_facts'],'context':[['Neri',['Neri was born in Elm.']]]}]))
    with pytest.raises(ValueError,match='sentence index out of range'):map_bundle(destination,official,maps)
