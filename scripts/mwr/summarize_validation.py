"""Rebuild the local real-model validation audit from saved observations."""
import hashlib, json, sqlite3
from collections import defaultdict, Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def read(path):
    return json.loads((ROOT/path).read_text())


def main():
    rounds=['smoke-r3','smoke-r3-repeat1','smoke-r3-repeat2']
    rows=[]
    for name in rounds:
        for line in (ROOT/'results/mwr'/name/'results.jsonl').read_text().splitlines():
            row=json.loads(line)
            rows.append({'round':name,**{k:row.get(k) for k in ['sample_id','method','status','answer_exact','prediction',
                'model_calls','input_tokens','output_tokens','live_state_tokens','windows_processed','reread_tokens']}})
    grouped=defaultdict(list)
    for row in rows: grouped[row['method']].append(row)
    groups={m:{'conditions':len(v),'correct':sum(r['answer_exact'] for r in v),
        'errors':sum(r['status']=='ERROR' for r in v),'statuses':dict(Counter(r['status'] for r in v))} for m,v in grouped.items()}
    dbs=list((ROOT/'results/mwr').glob('smoke*/experiment.sqlite'))
    for pattern in ['semantic-real-*.sqlite','semantic-holdout-*.sqlite','output-format-*.sqlite','diagnostics-dev_*.sqlite']:
        dbs.extend((ROOT/'reports/mwr').glob(pattern))
    calls=[]
    for path in sorted(dbs):
        con=sqlite3.connect(path)
        for item, in con.execute('select payload_json from model_call_observations'):
            call=json.loads(item);call['observation_file']=str(path.relative_to(ROOT));calls.append(call)
        con.close()
    physical=[c for c in calls if not c.get('cache_hit')]
    same_payload=defaultdict(list)
    for c in physical:
        if c['interface']!='ANSWER' or c.get('error'): continue
        payload_hash=hashlib.sha256(json.dumps(c['parameters'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        try: answer=json.loads(c['parsed_output'])
        except (ValueError,TypeError): continue
        same_payload[payload_hash].append({'run_id':c['run_id'],'file':c['observation_file'],
            'status':answer.get('status'),'answer':answer.get('answer')})
    divergent=[{'payload_sha256':h,'observations':v} for h,v in same_payload.items()
               if len({json.dumps([r['status'],r['answer']],sort_keys=True) for r in v})>1]
    heldout=read('reports/mwr/semantic-holdout-r3.json')
    supported=[r for r in heldout['rows'] if r.get('assessment',{}).get('status')=='SUPPORTED']
    report={'diagnostic_only':True,'rounds':rounds,'rows':rows,'method_groups':groups,
        'independent_questions':2,'repeat_observations_are_not_independent_questions':True,
        'semantic_holdout':{'correct':heldout['correct'],'count':heldout['count'],
            'supported_count':len(supported),'incorrect_supported_count':sum(not r['correct'] for r in supported),
            'metric_scope':'Concrete answer support/value and valid IDs; not a complete fidelity/coverage score.'},
        'identical_payload_divergence':divergent,
        'all_validation_usage':{'physical_attempts':len(physical),'cache_hits':sum(bool(c.get('cache_hit')) for c in calls),
            'failed_attempts':sum(bool(c.get('error')) for c in physical),
            'known_input_tokens':sum(c.get('input_tokens') or 0 for c in physical),
            'known_output_tokens':sum(c.get('output_tokens') or 0 for c in physical),
            'unknown_usage_attempts':sum(c.get('input_tokens') is None or c.get('output_tokens') is None for c in physical)},
        'limitations':['Two development questions only; no formal accuracy comparison.',
            'One sample lacks a reliable supplied-context mapping to its gold city.',
            'All fixed-candidate replay states fit the tested capacities; no retention advantage is established.',
            'Identical provider payloads produced different answers; no cause is inferred from this alone.'],
        'source_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in sorted((ROOT/'delaybind_core').rglob('*.py'))}}
    (ROOT/'reports/mwr/real-validation-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['method_groups','semantic_holdout','all_validation_usage']},ensure_ascii=False,indent=2))
    print('divergent identical-payload groups:',len(divergent))


if __name__=='__main__':main()
