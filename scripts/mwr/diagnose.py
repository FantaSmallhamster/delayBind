import argparse,asyncio,json,sys
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from delaybind_core.cli import _load_env_file,_api_config,_load_tokenizer
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.mwr.budget import CallBudget
from delaybind_core.mwr.diagnostics import diagnose_frozen_case
from delaybind_core.mwr.common import digest


def main():
    p=argparse.ArgumentParser(description='Explicit oracle diagnostics; never added to method result rows.')
    p.add_argument('--case',required=True,type=Path);p.add_argument('--config',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();raw=json.loads(a.config.read_text());cfg=ExperimentConfig.from_mapping(raw)
    case=json.loads(a.case.read_text());_load_env_file()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    store=SQLiteEventStore(str(a.output.with_suffix('.sqlite')))
    client=OpenAICompatibleClient(_api_config(raw),store=store)
    calls=CallBudget(client,None,_load_tokenizer(cfg.tokenizer_path),cfg.runner,'diagnostic-'+uuid4().hex,digest(case),lambda *x:None)
    report=asyncio.run(diagnose_frozen_case(case,cfg.runner,calls.tokenizer,calls))
    report['usage']=store.model_call_summary(calls.run_id)
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');store.close()


if __name__=='__main__':main()
