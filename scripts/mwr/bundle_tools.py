"""Offline-only import/audit/grading helpers. Never imported by inference."""
import hashlib
import json
import re
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath


def sha(data):
    return hashlib.sha256(data).hexdigest()


def normalized_question(text):
    return re.sub(r'\W+', '', unicodedata.normalize('NFKC', text).casefold())


def documents(context):
    headers=list(re.finditer(r'(?m)^Document ([1-9][0-9]*):\s*\n',context))
    if not headers or context[:headers[0].start()].strip():
        raise ValueError('missing/invalid document headers')
    if [int(h[1]) for h in headers]!=list(range(1,len(headers)+1)):
        raise ValueError('non-contiguous document headers')
    rows=[]
    for i,h in enumerate(headers):
        start=h.end();end=headers[i+1].start() if i+1<len(headers) else len(context)
        body=context[start:end]
        if not body.strip():raise ValueError('empty document')
        rows.append({'index':i,'title':body.split('\n',1)[0],
                     'context_start':start,'context_end':end,'text':body})
    return rows


def import_archive(archive,destination):
    """Verify archived checksums before writing; never execute bundled scripts."""
    archive,destination=Path(archive),Path(destination)
    with zipfile.ZipFile(archive) as z:
        names=z.namelist()
        if len(names)!=len(set(names)):raise ValueError('duplicate archive paths')
        roots={PurePosixPath(n).parts[0] for n in names}
        if len(roots)!=1:raise ValueError('expected one bundle root')
        root=next(iter(roots))+'/'
        files={}
        for info in z.infolist():
            p=PurePosixPath(info.filename)
            if p.is_absolute() or '..' in p.parts or (info.external_attr>>16)&0o170000==0o120000:
                raise ValueError('unsafe archive entry')
            if not info.is_dir():files[str(p.relative_to(root.rstrip('/')))]=z.read(info)
        checked=[]
        for line in files['metadata/SHA256SUMS.txt'].decode().splitlines():
            expected,name=line.split(maxsplit=1);name=name.strip()
            if name not in files or sha(files[name])!=expected:raise ValueError('checksum mismatch: '+name)
            checked.append(name)
        if set(files)-set(checked)-{'metadata/SHA256SUMS.txt'}:
            raise ValueError('unlisted bundle files')
        # A second import is read-only when bytes already match.
        for name,data in files.items():
            target=destination/name
            if target.exists() and target.read_bytes()!=data:
                raise ValueError('existing destination differs: '+str(target))
        for name,data in files.items():
            target=destination/name
            if not target.exists():
                target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    return {'archive_sha256':sha(archive.read_bytes()),'verified_files':checked,
            'file_sha256':{name:sha(data) for name,data in files.items()}}


def audit_bundle(bundle,development):
    bundle=Path(bundle)
    data=json.loads((bundle/'test/eval_2wikimultihopqa_50.json').read_text())
    gold=[json.loads(s) for s in (bundle/'grading/gold_answers.jsonl').read_text().splitlines() if s.strip()]
    old=json.loads(Path(development).read_text())
    def unique(rows,key):
        ids=[r[key] for r in rows]
        if len(ids)!=len(set(ids)):raise ValueError('duplicate '+key)
        return set(ids)
    ids=unique(data,'id');gold_ids=unique(gold,'sample_id');official=unique(gold,'official_id')
    if ids!=gold_ids:raise ValueError('test/gold sample IDs differ')
    by_id={g['sample_id']:g for g in gold};maps=[];types=Counter();doc_counts=Counter()
    from delaybind_core.mwr.common import digest
    for row in data:
        g=by_id[row['id']]
        if set(row)-{'id','type','input','context','num_docs'}:raise ValueError('unexpected inference fields')
        if row['input']!=g['question'] or row['type']!=g['question_type']:raise ValueError('question/type mismatch')
        if not g['gold_answers'] or not all(isinstance(a,str) and a.strip() for a in g['gold_answers']):
            raise ValueError('empty/invalid gold answers')
        docs=documents(row['context']);doc_counts[len(docs)]+=1;types[row['type']]+=1
        if row['num_docs']!=len(docs):raise ValueError('num_docs mismatch')
        indexes=g['evidence_idx']
        if not indexes or any(type(i)is not int or not 0<=i<len(docs) for i in indexes):
            raise ValueError('invalid evidence document index')
        indexes=list(dict.fromkeys(indexes))  # Several facts can share a document.
        support_titles={t for t,s in g['supporting_facts']}
        if not support_titles<={docs[i]['title'] for i in indexes}:raise ValueError('support title/index mismatch')
        if g['evidences_id'] and len(g['evidences'])!=len(g['evidences_id']):raise ValueError('nonempty triple/id count mismatch')
        if any(type(s)is not int or s<0 for t,s in g['supporting_facts']):raise ValueError('invalid sentence index')
        if any(len(e)!=3 for e in g['evidences']):raise ValueError('invalid evidence triple')
        mapped=[]
        for i in indexes:
            doc=docs[i]
            mapped.append({k:v for k,v in doc.items() if k!='text'}|{
                'doc_id':'d-'+digest(['',doc['text']])[:24],'source_hash':digest(doc['text']),
                'document_char_length':len(doc['text']),
                'official_sentence_indices':[s for t,s in g['supporting_facts'] if t==doc['title']],
                'document_mapping_status':'EXACT','sentence_mapping_status':'UNRESOLVED_NO_SENTENCE_ARRAY'})
        maps.append({'sample_id':row['id'],'official_id':g['official_id'],'support_documents':mapped,
                     't_star_status':'NOT_DERIVED_FROM_METHOD_BINDINGS'})
    overlaps={
        'sample_ids':sorted(ids&{r['id'] for r in old}),
        'official_ids':sorted(official&{r['official_id'] for r in old if r.get('official_id')}),
        'normalized_questions':sorted({normalized_question(r['input']) for r in data}&
                                     {normalized_question(r.get('input',r.get('question',''))) for r in old}),
        'context_sha256':sorted({sha(r['context'].encode()) for r in data}&{sha(r['context'].encode()) for r in old})}
    report={'sample_count':len(data),'gold_count':len(gold),'type_counts':dict(types),
        'document_count_distribution':dict(doc_counts),'overlap_with_development':overlaps,
        'support_fact_count':sum(len(g['supporting_facts']) for g in gold),
        'evidence_triple_count':sum(len(g['evidences']) for g in gold),
        'samples_without_optional_evidence_entity_ids':sum(not g['evidences_id'] for g in gold),
        'samples_with_repeated_support_document_indices':sum(len(g['evidence_idx'])!=len(set(g['evidence_idx'])) for g in gold),
        'support_document_count':sum(len(m['support_documents']) for m in maps),
        'all_support_titles_match_zero_based_document_indices':True,
        'sentence_mapping':'Unavailable: original sentence arrays absent; no guessed punctuation splitting.',
        'selection_metadata':json.loads((bundle/'metadata/selection_summary.json').read_text()),
        'scope':'Structural and overlap audit, not manual verification of every semantic support claim.',
        'test_sha256':sha((bundle/'test/eval_2wikimultihopqa_50.json').read_bytes()),
        'gold_sha256':sha((bundle/'grading/gold_answers.jsonl').read_bytes())}
    return report,maps


def bundle_normalize(value):
    if isinstance(value,bool):return 'yes' if value else 'no'
    text=' '.join(re.sub(r'[^\w\s]',' ','' if value is None else str(value).casefold()).split())
    return {'true':'yes','false':'no'}.get(text,text)


def bundle_score(value,answers):
    p=bundle_normalize(value);pt=p.split();best=0.
    for answer in answers:
        gt=bundle_normalize(answer).split();overlap=sum((Counter(pt)&Counter(gt)).values())
        if pt and gt:best=max(best,2*overlap/(len(pt)+len(gt)))
    return {'exact':bool(p) and p in [bundle_normalize(a) for a in answers],'token_f1':best}


def grade_rows(predictions,gold,methods,orders):
    """Full frozen-set denominator for every declared method/order."""
    from delaybind_core.mwr.scoring import score
    by_id={g['sample_id']:g for g in gold}
    if len(by_id)!=len(gold):raise ValueError('duplicate gold ID')
    if not methods or not orders or len(set(methods))!=len(methods) or len(set(orders))!=len(orders):
        raise ValueError('methods/orders must be nonempty and unique')
    indexed={}
    for row in predictions:
        key=(row['sample_id'],row['method'],row['order'])
        if key[0] not in by_id or key[1] not in methods or key[2] not in orders:raise ValueError('unknown prediction condition')
        if key in indexed:raise ValueError('duplicate prediction condition')
        indexed[key]=row
    scored=[];groups=[]
    for method in methods:
        for order in orders:
            group=[]
            for sid,g in by_id.items():
                row=indexed.get((sid,method,order));answer=(row or {}).get('answer')
                status=(row or {}).get('status','MISSING')
                value=(row or {}).get('prediction',answer.get('answer') if isinstance(answer,dict) else answer)
                if status not in {'ANSWERED','OK'} or isinstance(answer,dict) and answer.get('status')!='ANSWERED':value=None
                unified=score(value,g['gold_answers']);provided=bundle_score(value,g['gold_answers'])
                result={'sample_id':sid,'original_question_id':g.get('official_id',sid),
                    'method':method,'order':order,'status':status,'submitted':row is not None,
                    'prediction':value,**unified,'bundle_answer_exact':provided['exact'],'bundle_answer_token_f1':provided['token_f1']}
                group.append(result);scored.append(result)
            n=len(group);groups.append({'method':method,'order':order,'count':n,
                'submitted':sum(r['submitted'] for r in group),'missing':sum(not r['submitted'] for r in group),
                'primary_mwr_em':sum(r['answer_exact'] for r in group)/n if n else None,
                'primary_mwr_f1':sum(r['answer_token_f1'] for r in group)/n if n else None,
                'bundle_em':sum(r['bundle_answer_exact'] for r in group)/n if n else None,
                'bundle_f1':sum(r['bundle_answer_token_f1'] for r in group)/n if n else None,
                'status_counts':dict(Counter(r['status'] for r in group))})
    return {'groups':groups,'rows':scored,'primary_metric':'mwr-unified-v1',
        'secondary_metric':'supplied-bundle normalization (keeps articles; true/false to yes/no)',
        'failed_or_missing_conditions_score_zero':True}
