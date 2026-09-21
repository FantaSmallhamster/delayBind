"""Both graphs are deterministic projections of the same BindingStore."""

import re

from .schema_v52 import QueryDependencyEdge, SubqueryStateV52, digest, use_key


def query_by_id(state, qid):
    return next(q for q in state.plan.queries if q.id == qid)


def parent_bindings(state, qid):
    result = []
    for parent in query_by_id(state, qid).depends_on:
        bid = state.executions[parent].current_binding_id
        if bid is None or not state.binding_store[bid].valid:
            return None
        result.append(state.binding_store[bid])
    return result


def input_signature(state, qid):
    parents = parent_bindings(state, qid)
    if parents is None:
        return "DORMANT"
    version = state.executions[qid].version
    return "I" + digest([version, [(p.binding_id, p.binding_revision) for p in parents]])


def descendants(state, roots):
    affected = set(roots)
    while True:
        more = {q.id for q in state.plan.queries if set(q.depends_on) & affected}
        if more <= affected:
            return affected
        affected |= more


def proof_fact_ids(state, binding_id):
    binding = state.binding_store[binding_id]
    result = set(binding.direct_fact_ids)
    for parent in binding.parent_binding_ids:
        result |= proof_fact_ids(state, parent)
    return result


def support_use_keys(state, binding_id):
    binding = state.binding_store[binding_id]
    if binding.direct_fact_ids:
        return [use_key(binding.producer_query_id, binding.input_signature, f) for f in binding.direct_fact_ids]
    return sorted({key for parent in binding.parent_binding_ids for key in support_use_keys(state, parent)})


def query_projection(state):
    queries = []
    for q in state.plan.queries:
        execution = state.executions[q.id]
        bound = {}
        for variable, parent in q.inputs.items():
            bid = state.executions[parent].current_binding_id
            if bid and state.binding_store[bid].valid:
                b = state.binding_store[bid]
                bound[variable] = {"binding_id": bid, "value": b.value, "revision": b.binding_revision}
        import json
        rendered = re.sub(r"\?[A-Za-z_][A-Za-z_0-9]*", lambda m: (
            json.dumps(bound[m[0]]["value"], ensure_ascii=False) if m[0] in bound else m[0]), q.template)
        queries.append({**q.model_dump(), **execution.model_dump(), "rendered_query": rendered,
                        "bound_inputs": bound})
    return {"schema_version": "v3", "queries": queries,
            "edges": [e.model_dump() for e in state.query_edges]}


def rebuild_navigation(state: SubqueryStateV52) -> None:
    edges, links = [], []
    for q in state.plan.queries:
        for variable, parent in sorted(q.inputs.items()):
            bid = state.executions[parent].current_binding_id
            binding = state.binding_store.get(bid) if bid else None
            edge = QueryDependencyEdge(producer_query_id=parent, consumer_query_id=q.id, variable=variable,
                                       binding_id=bid, binding_revision=binding.binding_revision if binding else None,
                                       value=binding.value if binding else None)
            edges.append(edge)
            if binding is None:
                continue
            sources = support_use_keys(state, bid)
            for key, use in sorted(state.uses.items()):
                if (use.query_id == q.id and use.input_signature == state.executions[q.id].input_signature
                    and use.status == "ACCEPTED"):
                    links.append({"from": sources, "to": key, "binding_id": bid,
                                  "binding_revision": binding.binding_revision,
                                  "support_group_id": f"SG:{bid}", "conjunction": True,
                                  "variable": variable, "value": binding.value})
    state.query_edges = edges
    state.binding_ports = [e.model_copy(deep=True) for e in edges]
    state.navigation_links = links


def assert_dual_graph_invariants(state: SubqueryStateV52) -> None:
    expected = state.model_copy(deep=True)
    rebuild_navigation(expected)
    if (expected.query_edges != state.query_edges or expected.binding_ports != state.binding_ports
            or expected.navigation_links != state.navigation_links):
        raise ValueError("DUAL_GRAPH_INCONSISTENCY")
    for qid, execution in state.executions.items():
        if execution.input_signature != input_signature(state, qid):
            raise ValueError(f"STALE_INPUT_SIGNATURE:{qid}")
        bid = execution.current_binding_id
        if (execution.status == "RESOLVED") != bool(bid):
            raise ValueError("RESOLUTION_BINDING_MISMATCH")
        if bid and (not state.binding_store[bid].valid or state.binding_store[bid].producer_query_id != qid):
            raise ValueError("INVALID_CURRENT_BINDING")
    for binding in state.binding_store.values():
        if not binding.valid:
            continue
        ex = state.executions[binding.producer_query_id]
        if ex.current_binding_id != binding.binding_id or ex.input_signature != binding.input_signature:
            raise ValueError("STALE_BINDING")
        refs = set()
        for fid in binding.direct_fact_ids:
            use = state.uses[use_key(binding.producer_query_id, binding.input_signature, fid)]
            if use.status != "ACCEPTED" or not set(state.facts[fid].source_refs) <= set(use.reviewed_source_refs):
                raise ValueError("BINDING_SUPPORT_NOT_REVIEWED")
            refs.update(state.facts[fid].source_refs)
        for bid in binding.parent_binding_ids:
            parent = state.binding_store[bid]
            if not parent.valid:
                raise ValueError("INVALID_PARENT_PROOF")
            refs.update(parent.source_refs)
        if not refs or refs != set(binding.source_refs):
            raise ValueError("BINDING_RAW_COVERAGE")
