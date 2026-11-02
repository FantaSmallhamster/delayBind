import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.mwr.bundle_tools import grade_rows,sha


def main():
    p=argparse.ArgumentParser(description='Join predictions with separate gold only after inference; retain all missing conditions.')
    p.add_argument('--gold',type=Path,required=True);p.add_argument('--results',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--methods',nargs='+',required=True);p.add_argument('--orders',nargs='+',default=['original'])
    a=p.parse_args();read=lambda path:[json.loads(s) for s in path.read_text().splitlines() if s.strip()]
    report=grade_rows(read(a.results),read(a.gold),a.methods,a.orders)
    report['input_hashes']={'results':sha(a.results.read_bytes()),'gold':sha(a.gold.read_bytes())}
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report['groups'],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
