import re
from .common import ProtocolError, dump
from .models import Plan

VARIABLE = re.compile(r'\?[A-Za-z_][A-Za-z0-9_]*')


def validate_plan(plan):
    ids = [q.id for q in plan.queries]
    outputs = [q.output for q in plan.queries]
    if len(ids) != len(set(ids)) or len(outputs) != len(set(outputs)):
        raise ProtocolError('query IDs and output variables must be unique')
    by_id = {q.id:q for q in plan.queries}
    seen, stack = set(), set()
    def visit(q):
        if q.id in stack: raise ProtocolError('cyclic plan')
        if q.id in seen: return
        if not VARIABLE.fullmatch(q.output): raise ProtocolError('output must be a variable')
        stack.add(q.id)
        for dep in q.depends_on:
            if dep not in by_id: raise ProtocolError('unknown dependency')
            visit(by_id[dep])
        known = {by_id[dep].output for dep in ancestors(q.id, by_id)}
        if set(VARIABLE.findall(q.template)) - known:
            raise ProtocolError('template input lacks upstream dependency')
        stack.remove(q.id); seen.add(q.id)
    for q in plan.queries: visit(q)
    return plan


def ancestors(qid, by_id):
    result, pending = set(), list(by_id[qid].depends_on)
    while pending:
        dep = pending.pop()
        if dep not in result:
            result.add(dep)
            pending.extend(by_id[dep].depends_on)
    return result


def projection(state, include_dormant=True):
    values = {q.output:state.bindings[q.id].value for q in state.plan.queries if q.id in state.bindings}
    rows=[]
    for q in state.plan.queries:
        unknown=[v for v in VARIABLE.findall(q.template) if v not in values]
        ready=not unknown and all(dep in state.bindings for dep in q.depends_on)
        status='RESOLVED' if q.id in state.bindings else ('ACTIVE' if ready else 'DORMANT')
        if status=='DORMANT' and not include_dormant: continue
        rows.append({**q.model_dump(), 'rendered_query':VARIABLE.sub(lambda m: str(values[m[0]]) if m[0] in values else m[0], q.template),
                     'status':status, 'unknown_inputs':unknown,
                     'known_inputs':{v:values[v] for v in VARIABLE.findall(q.template) if v in values},
                     'state_version':state.version})
    return rows


def reading_messages(question, rows, state, config):
    return [{'role':'user','content':
        'Read only this forward window. Propose concise source-backed cues relevant to current or future relation demands. '
        'Preserve negation, time, identity and scope. Unknown variables may match any entity: never guess their values. '
        'Use candidate_query_ids=[] if routing is uncertain; one cue may serve multiple queries. '
        'Do not emit confirmed answers. Plan notes are not evidence. Do not broadly extract every biography. '
        'Return an evidence array with source_refs, cue_text, entity_mentions, relation_cues, candidate_query_ids, plus plan_notes. '
        'Facts must go in evidence, never only in plan_notes. An empty demands list still requires extracting facts relevant to the original question.\n'
        +dump({'question':question,'window':rows,'demands':projection(state,config.show_dormant_demands),
               'memory':[e.model_dump() for e in state.evidence.values()]})}]
