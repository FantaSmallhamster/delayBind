import argparse,asyncio,json,sys
import hashlib
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from delaybind_core.cli import _load_env_file,_api_config,_load_tokenizer
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr.models import Assessment
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.common import digest,dump
from delaybind_core.mwr.scoring import normalize
from delaybind_core.mwr.binding import assessment_messages
from delaybind_core.mwr.source_access import span_id
from delaybind_core.mwr.models import SourceSpan, EvidenceRecord
from delaybind_core.mwr import PROMPT_VERSION


async def check(cases,calls):
    rows=[]
    for case in cases:
        fp=digest({k:v for k,v in case.items() if not k.startswith('expected')})
        span=SourceSpan(doc_id='d-semantic',start=0,end=len(case['text']),source_hash=digest(case['text']))
        evidence=EvidenceRecord(evidence_id='e1',source_spans=[span],cue_text=case['cue'],observed_window=1)
        messages=assessment_messages({'question_instance':{'rendered_query':case['question']},'input_fingerprint':fp,
            'upstream_support':{},'candidates':[evidence.model_dump()],
            'raw_sources':[{'source_ref':span_id(span),**span.model_dump(),'text':case['text'],'neighborhood':False}]})
        try:
            answer=await calls.invoke('MEMORY',messages,Assessment)
            correct=answer.input_fingerprint==fp and set(answer.support_ids)<={'e1'} and answer.status==case['expected_status']
            if answer.status=='SUPPORTED': correct=correct and bool(answer.support_ids) and normalize(answer.value)==normalize(case['expected_value'])
            rows.append({'case_id':case['id'],'correct':bool(correct),'assessment':answer.model_dump(),
                         'expected_status':case['expected_status'],'expected_value':case['expected_value']})
        except Exception as exc:
            rows.append({'case_id':case['id'],'correct':False,'error_type':type(exc).__name__,'error':str(exc)})
    return {'diagnostic_only':True,'prompt_version':PROMPT_VERSION,'count':len(rows),'correct':sum(r['correct'] for r in rows),'rows':rows}


def main():
    p=argparse.ArgumentParser(description='Independent synthetic natural-language semantic checks against the real model.')
    p.add_argument('--config',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--cases',type=Path,default=ROOT/'tests/mwr/semantic_cases.json')
    a=p.parse_args();raw=json.loads(a.config.read_text());cfg=ExperimentConfig.from_mapping(raw);_load_env_file()
    a.output.parent.mkdir(parents=True,exist_ok=True);store=SQLiteEventStore(str(a.output.with_suffix('.sqlite')))
    calls=CallBudget(OpenAICompatibleClient(_api_config(raw),store=store),None,_load_tokenizer(cfg.tokenizer_path),cfg.runner,'semantic-'+uuid4().hex,'semantic-suite-v1',lambda *x:None)
    report=asyncio.run(check(json.loads(a.cases.read_text()),calls))
    report['cases_sha256']=hashlib.sha256(a.cases.read_bytes()).hexdigest()
    report['source_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'delaybind_core/mwr').glob('*.py'))}
    report['usage']=store.model_call_summary(calls.run_id)
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');store.close()


if __name__=='__main__':main()
