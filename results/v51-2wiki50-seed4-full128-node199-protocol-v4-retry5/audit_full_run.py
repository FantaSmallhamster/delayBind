"""Audit saved full benchmark results without making model requests."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3

OUT = Path(__file__).resolve().parent
ROOT = OUT.parent.parent
read = lambda path: json.loads(path.read_text())
audit = read(OUT / 'benchmark.audit.json')
comparison = read(OUT / 'baseline_comparison.json')
rows = [json.loads(line) for line in (OUT / 'results.jsonl').read_text().splitlines()]
db = sqlite3.connect(f'file:{OUT}/experiment.sqlite?mode=ro', uri=True)
event_counts, interfaces, verdicts, failures, peers = (Counter() for _ in range(5))
violations, per_question, repairs = [], [], []
for row in comparison['rows']:
    trajectory = read(ROOT / row['trajectory'])
    sid = row['sample_id']
    state = trajectory.get('state') or {}
    calls = trajectory['model_calls']
    counts = Counter(e['event_type'] for e in trajectory['events'])
    event_counts.update(counts)
    interfaces.update(c['interface'] for c in calls)
    failures.update(c['error'] for c in calls if c.get('error'))
    peers.update(c['raw_response']['_client_transport']['peer_ip'] for c in calls if not c.get('error'))
    facts, deferred, lookups, selections, reviews = {}, set(), {}, {}, {}
    for e in trajectory['events']:
        typ, p = e['event_type'], e['payload']
        if typ == 'FACT_STORED':
            facts[p['fact']['fact_id']] = p['fact']
        elif typ == 'FACT_DEFERRED':
            deferred.add((p['use']['query_id'], p['use']['fact_id']))
        elif typ == 'DEFER_WORKSPACE_LOOKUP':
            lookups[p['query_id']] = set(p['fact_ids'])
        elif typ == 'DEFER_CANDIDATES_SELECTED':
            selections[p['query_id']] = set(p['fact_ids'])
            if not set(p['fact_ids']) <= lookups.get(p['query_id'], set()):
                violations.append({'sample_id':sid, 'reason':'selection outside current defer workspace lookup', 'event_seq':e['event_seq']})
        elif typ == 'DEFER_SOURCES_FETCHED':
            expected = {ref for fid in p['fact_ids'] for ref in facts[fid]['source_refs']}
            if set(p['source_refs']) != expected or not set(p['fact_ids']) <= selections.get(p['query_id'], set()):
                violations.append({'sample_id':sid, 'reason':'source fetch outside selected deferred candidates', 'event_seq':e['event_seq']})
        elif typ == 'CANDIDATE_REVIEWED':
            if p['fact_id'] not in selections.get(p['query_id'], set()):
                violations.append({'sample_id':sid, 'reason':'review outside selected deferred candidates', 'event_seq':e['event_seq']})
            verdicts[p['verdict'] + ':' + p.get('verdict_source','MODEL')] += 1
            reviews[p['query_id'], p['fact_id']] = p['verdict']
        elif typ == 'FACT_PROMOTED':
            u=p['use']
            if reviews.get((u['query_id'], u['fact_id'])) != 'MATCH':
                violations.append({'sample_id':sid, 'reason':'promotion without MATCH', 'event_seq':e['event_seq']})
        elif typ == 'UPDATE_REPAIR_REQUESTED':
            repairs.append({'sample_id':sid, **p})
    # Include failed calls: the request still must respect visible-source scope.
    earlier_reading = ''
    for call in calls:
        prompt='\n'.join(m['content'] for m in call['raw_request']['messages'])
        if call['interface']=='UPDATE':
            earlier_reading += '\n' + prompt
        if call['interface']!='VERIFY':
            continue
        section = prompt.split('\nSelected deferred candidates:\n',1)[1].split('\n\nTheir already-read original sources:\n',1)[0]
        expected = set()
        for line in section.splitlines():
            if re.match(r'^F[0-9a-f]{16} \| ',line):
                fid, refs, _ = line.split(' | ',2)
                expected.update(refs.split(','))
        for ref in expected:
            saved=db.execute('select payload_json from raw_archive where run_id=? and source_ref=?',(trajectory['run_id'],ref)).fetchone()
            text=json.loads(saved[0])['text'].strip() if saved else ''
            if not text or text not in prompt or text not in earlier_reading:
                violations.append({'sample_id':sid,'reason':'VERIFY source absent from raw archive or earlier reading','source_ref':ref})
    per_question.append({
        **{k:row.get(k) for k in ['sample_id','status','error_type','error','em','f1','upstream_parsed_answer','gold_first','protocol_valid','windows','unresolved_queries']},
        'event_counts':dict(counts),
        'working_memory_fact_count':len(state.get('working_memory',{}).get('facts',[])) if state else None,
        'protocol_failures':[e['payload'] for e in trajectory['events'] if e['event_type']=='PROTOCOL_REPAIR_EXHAUSTED'],
        'queries':[{'id':q['id'],'status':q['status'],'result':q['result']} for q in state.get('queries',[])],
    })
changed=[name for name,h in audit['code_hashes'].items() if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=h]
result={
    'count':len(rows),'answered':sum(r.get('runtime_status')=='ANSWERED' for r in rows),
    'errors':sum(r['status']!='OK' for r in rows),
    'runtime_event_counts':dict(event_counts),'request_counts_by_interface':dict(interfaces),
    'verify_verdict_counts':dict(verdicts),'api_failure_messages':dict(failures),
    'successful_peer_counts':dict(peers),'defer_source_flow_violations':violations,
    'frozen_code_changes':changed,'planned_windows':sum(s['windows'] for s in audit['selected']),
    'reported_processed_windows':sum(r.get('windows_processed') or 0 for r in rows),
    'per_question':per_question,'local_repairs':repairs,
    'known_label_anomaly':{'sample_id':'dev_1476','reason':'question asks nationality; frozen first gold is Chill Factor; unchanged source label, original score retained'},
}
(OUT/'offline_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
assert len(rows)==128 and len({r['sample_id'] for r in rows})==128
assert not violations, violations
assert not changed, changed
print(json.dumps({k:v for k,v in result.items() if k not in ['per_question','local_repairs']},ensure_ascii=False,indent=2))
