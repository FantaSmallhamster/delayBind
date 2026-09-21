"""One binding authority, shared effective predicates and dual-graph projections."""

import re
from .schema_v52 import digest
from .schema_r2 import use_key


def query(state, qid):
    return next(q for q in state.plan.queries if q.id == qid)


def topological(state):
    ordered, seen = [], set()
    def visit(qid):
        if qid in seen:
            return
        for parent in query(state, qid).depends_on:
            visit(parent)
        seen.add(qid)
        ordered.append(qid)
    for q in state.plan.queries:
        visit(q.id)
    return ordered


def descendants(state, roots):
    found = set(roots)
    while True:
        more = {q.id for q in state.plan.queries if set(q.depends_on) & found}
        if more <= found:
            return found - set(roots)
        found |= more


def input_signature(state, qid):
    q, ex = query(state, qid), state.executions[qid]
    return "I" + digest([ex.version, sorted((slot, parent, state.executions[parent].current_binding_id)
                                            for slot, parent in q.inputs.items())])


def binding_effective(state, bid, visiting=None):
    b = state.binding_store.get(bid)
    if b is None or not b.valid:
        return False
    seen = set() if visiting is None else set(visiting)
    if bid in seen:
        return False
    seen.add(bid)
    ex = state.executions[b.producer_query_id]
    if (ex.current_binding_id != bid or ex.version != b.query_version
            or ex.input_signature != b.input_signature or ex.blocking_review_ids):
        return False
    if any(not binding_effective(state, p, seen) for p in b.parent_binding_ids):
        return False
    return all(key in state.uses and state.uses[key].status == "ACCEPTED"
               and state.uses[key].query_version == ex.version
               and state.uses[key].input_signature == ex.input_signature for key in b.direct_use_ids)


def query_ready(state, qid):
    return all(binding_effective(state, state.executions[p].current_binding_id)
               for p in query(state, qid).depends_on)


def current_uses(state, qid):
    ex = state.executions[qid]
    return [u for u in state.uses.values() if u.query_id == qid and u.query_version == ex.version
            and u.input_signature == ex.input_signature and u.status != "INVALIDATED"]


def use_effective(state, use):
    ex = state.executions[use.query_id]
    return (use.status == "ACCEPTED" and use.query_version == ex.version
            and use.input_signature == ex.input_signature and query_ready(state, use.query_id)
            and not ex.blocking_review_ids)


def proof_refs(state, bid):
    b = state.binding_store[bid]
    return set(b.source_refs)


def proof_facts(state, bid):
    b = state.binding_store[bid]
    return set(b.direct_fact_ids) | {fid for p in b.parent_binding_ids for fid in proof_facts(state, p)}


def refresh(state):
    for qid in topological(state):
        ex = state.executions[qid]
        ex.input_signature = input_signature(state, qid)
        ex.status = ("RESOLVED" if ex.current_binding_id else "ACTIVE" if query_ready(state, qid) else "DORMANT")
    rebuild(state)


def query_projection(state):
    rows = []
    for q in state.plan.queries:
        ex = state.executions[q.id]
        bound = {slot: state.binding_store[state.executions[parent].current_binding_id].value
                 for slot, parent in q.inputs.items() if binding_effective(state, state.executions[parent].current_binding_id)}
        row = {**q.model_dump(), **ex.model_dump(),
                     "rendered_query": re.sub(r"\?[A-Za-z_][A-Za-z_0-9]*", lambda m: str(bound.get(m[0], m[0])), q.template),
                     "bound_inputs": bound, "review_pending": bool(ex.blocking_review_ids),
                     "effective": binding_effective(state, ex.current_binding_id),
                     "current_value": state.binding_store[ex.current_binding_id].value if ex.current_binding_id else None}
        from .member_graph_r2 import enabled
        if enabled(state):
            # Extraction/recall see the demand template, not a list pasted into
            # an entity slot. MEMORY gets one fully instantiated branch later.
            row.update(member_bindings=True, rendered_query=q.template, bound_inputs={})
        rows.append(row)
    return {"queries": rows, "edges": state.query_edges}


def rebuild(state):
    edges, links = [], []
    for q in state.plan.queries:
        for slot, parent in sorted(q.inputs.items()):
            bid = state.executions[parent].current_binding_id
            b = state.binding_store.get(bid)
            edges.append(dict(producer_query_id=parent, consumer_query_id=q.id, variable=slot,
                              binding_id=bid, binding_revision=b.binding_revision if b else None,
                              value=b.value if b else None, effective=binding_effective(state, bid)))
    for u in state.uses.values():
        if not use_effective(state, u):
            continue
        for parent in query(state, u.query_id).depends_on:
            bid = state.executions[parent].current_binding_id
            b = state.binding_store[bid]
            links.append(dict(binding_id=bid, target_use_id=u.use_id, target_fact_id=u.fact_id,
                              source_use_ids=b.direct_use_ids, support_group=bid,
                              query_id=u.query_id, input_signature=u.input_signature))
    from .member_graph_r2 import enabled
    if enabled(state):
        # Aggregate query ports are routing metadata, not entity proof edges.
        # Never attach every member of a parent query to each child's fact.
        links = []
        active = [b for b in state.binding_store.values() if binding_effective(state, b.binding_id)]
        by_member = {m.member_id: (b, m) for b in active for m in b.members}
        for b in active:
            for m in b.members:
                for parent_id in m.parent_member_ids:
                    parent_binding, parent = by_member[parent_id]
                    source_uses = [uid for uid in parent_binding.direct_use_ids
                                   if state.uses[uid].fact_id in parent.direct_fact_ids]
                    for uid in b.direct_use_ids:
                        u = state.uses[uid]
                        if u.fact_id in m.direct_fact_ids:
                            links.append(dict(binding_id=parent_binding.binding_id, target_use_id=uid,
                                target_fact_id=u.fact_id, source_use_ids=source_uses, support_group=parent_id,
                                query_id=b.producer_query_id, input_signature=b.input_signature,
                                source_member_id=parent_id, target_member_id=m.member_id))
    state.query_edges = edges
    state.binding_ports = [dict(e) for e in edges]
    state.navigation_links = sorted(links, key=lambda x: (x["target_use_id"], x["binding_id"], x.get("target_member_id", ""), x.get("source_member_id", "")))
    reverse = {}
    for key, u in state.uses.items():
        reverse.setdefault(u.fact_id, []).append(key)
    state.reverse_fact_uses = {fid: sorted(keys) for fid, keys in reverse.items()}


def assert_invariants(state):
    from .member_graph_r2 import enabled, assert_member_invariants
    if enabled(state):
        assert_member_invariants(state)
    projected = state.model_copy(deep=True)
    rebuild(projected)
    if any(getattr(state, name) != getattr(projected, name) for name in
           ("query_edges", "binding_ports", "navigation_links", "reverse_fact_uses")):
        raise ValueError("DUAL_GRAPH_INCONSISTENCY")
    for qid, ex in state.executions.items():
        if ex.input_signature != input_signature(state, qid):
            raise ValueError("STALE_INPUT_SIGNATURE")
        expected = "RESOLVED" if ex.current_binding_id else "ACTIVE" if query_ready(state, qid) else "DORMANT"
        if ex.status != expected:
            raise ValueError("QUERY_STATUS_INCONSISTENCY")
        for rid in ex.blocking_review_ids:
            if rid not in state.reviews or state.reviews[rid].status in {"DONE", "CANCELLED"}:
                raise ValueError("ORPHAN_REVIEW_BARRIER")
    for b in state.binding_store.values():
        if not b.valid:
            continue
        ex = state.executions[b.producer_query_id]
        if ex.current_binding_id != b.binding_id or ex.version != b.query_version or ex.input_signature != b.input_signature:
            raise ValueError("STALE_BINDING")
        parents = [state.executions[p].current_binding_id for p in query(state, b.producer_query_id).depends_on]
        if sorted(parents) != sorted(b.parent_binding_ids):
            raise ValueError("PARENT_PROOF_MISMATCH")
        refs, facts = set(), set()
        for key in b.direct_use_ids:
            u = state.uses[key]
            if key != use_key(u.query_id, u.query_version, u.input_signature, u.fact_id) or u.status != "ACCEPTED":
                raise ValueError("BINDING_SUPPORT_NOT_ACCEPTED")
            if u.query_id != b.producer_query_id or u.query_version != b.query_version or u.input_signature != b.input_signature:
                raise ValueError("BINDING_USE_VERSION_MISMATCH")
            fact = state.facts[u.fact_id]
            if not set(fact.source_refs) <= set(u.reviewed_source_refs):
                raise ValueError("BINDING_SUPPORT_NOT_REVIEWED")
            refs.update(fact.source_refs)
            facts.add(u.fact_id)
        for pid in parents:
            if not state.binding_store[pid].valid:
                raise ValueError("RETIRED_PARENT_PROOF")
            refs.update(state.binding_store[pid].source_refs)
        if ((not refs and not state.fact_only) or refs != set(b.source_refs)
                or facts != set(b.direct_fact_ids)):
            raise ValueError("BINDING_PROOF_CLOSURE")
    live_keys = [j.job_key for j in state.jobs.values() if j.status not in {"CANCELLED", "DONE"}]
    if len(live_keys) != len(set(live_keys)):
        raise ValueError("DUPLICATE_DURABLE_JOB")
