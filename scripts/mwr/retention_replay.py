import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from delaybind_core.mwr.experiments import retention_replay
from delaybind_core.tokenization import TokenizerJSON


def main():
    p=argparse.ArgumentParser(description='Frozen predicted candidate-stream retention ablation (offline only).')
    p.add_argument('--stream',required=True,type=Path);p.add_argument('--tokenizer',required=True)
    p.add_argument('--budgets',nargs='+',type=int,default=[2000,4000,8000]);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();raw=json.loads(a.stream.read_text())
    if any(b<=0 for b in a.budgets):p.error('budgets must be positive')
    report=retention_replay(raw['plan'],raw['windows'],TokenizerJSON(a.tokenizer),a.budgets)
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
