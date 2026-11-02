"""Offline trace/paired analysis. Annotations are never provided to inference."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from delaybind_core.mwr.analysis import DelayedAnnotation,trace_metrics,paired_bootstrap


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--results',required=True,type=Path)
    p.add_argument('--annotations',type=Path)
    p.add_argument('--method-a',default='delaybind_mwr');p.add_argument('--method-b',default='flat_memory_mwr')
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    rows=[json.loads(line) for line in args.results.read_text().splitlines() if line.strip()]
    report={'paired':paired_bootstrap(rows,args.method_a,args.method_b),'process':[]}
    annotations=json.loads(args.annotations.read_text()) if args.annotations else {}
    for row in rows:
        key=row['sample_id']+':'+row['order']
        if key in annotations:
            trace=json.loads(Path(row['trajectory_path']).read_text())
            report['process'].append({'sample_id':row['sample_id'],'method':row['method'],'order':row['order'],
                **trace_metrics(trace['events'],[DelayedAnnotation(**a) for a in annotations[key]],row['answer_exact'])})
    report['annotation_status']='provided' if args.annotations else 'not provided; no process accuracy claimed'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__': main()
