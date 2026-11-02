"""Small controlled provider-format probe, separate from method evaluation."""
import argparse, asyncio, getpass, json, os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from delaybind_core.cli import _api_config
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr.answering import answer_request, AnswerContract
from delaybind_core.mwr.models import Answer
from delaybind_core.mwr.common import dump


async def probe(client, archived=None):
    schema=Answer.model_json_schema()
    schema['required']=list(schema['properties'])
    request=[{'role':'system','content':'Return the final JSON object only. Do not place reasoning in any field. Output schema:\n'+dump(schema)},
        *answer_request('Where was Neri born?',{'raw_sources':[{'source_ref':'s1','text':'Neri was born in Elm. Neri now lives in Ash.'}],
            'supported_relations':[],'unresolved_demands':[],'conflicts':[]},AnswerContract())]
    if archived:
        schema=archived['parameters']['response_format']['json_schema']['schema']
        request=archived['parameters']['messages']
    rows=[]
    for mode in ['json_schema','json_object']:
        raw=await client.complete(run_id='format-probe-'+mode,interface='ANSWER',messages=request,
            response_schema=schema,extra={} if mode=='json_schema' else {'response_format':{'type':'json_object'}})
        try:
            a=Answer.model_validate_json(raw)
            valid=(a.status=='ANSWERED' and a.answer=='Elm' and a.source_refs==['s1']) if not archived else None
            rows.append({'mode':mode,'correct':valid,'answer':a.model_dump()})
        except ValueError as e: rows.append({'mode':mode,'correct':False,'error':str(e)[:300]})
    return rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--trajectory',type=Path)
    args=p.parse_args()
    if not os.environ.get('MODEL_API_KEY'):os.environ['MODEL_API_KEY']=getpass.getpass('API key (hidden, memory only): ')
    store=SQLiteEventStore(str(args.output.with_suffix('.sqlite')))
    client=OpenAICompatibleClient(_api_config(json.loads(args.config.read_text())),store=store)
    archived=None
    if args.trajectory:
        archived=next(c for c in json.loads(args.trajectory.read_text())['model_calls'] if c['interface']=='ANSWER')
    rows=asyncio.run(probe(client,archived))
    report={'diagnostic_only':True,'rows':rows,'usage':{m:store.model_call_summary('format-probe-'+m) for m in ['json_schema','json_object']}}
    args.output.write_text(json.dumps(report,indent=2)+'\n');store.close()
    print(json.dumps(rows))


if __name__=='__main__':main()
