"""Raw-backed navigation views. Defer buckets never enter an ANSWER view."""

from .navigation_graph import proof_fact_ids
from .schema_v52 import EvidencePackV52


def render_fact_memory(state, archive, *, pending_keys=(), extra_refs=(), final=False, query_id=None, reader=False):
    pinned = set()
    for b in state.binding_store.values():
        if b.valid:
            pinned |= proof_fact_ids(state, b.binding_id)
    nodes, diagnostics, refs = [], [], set(extra_refs)
    for key, use in sorted(state.uses.items()):
        if use.input_signature != state.executions[use.query_id].input_signature:
            continue
        accepted = use.status == "ACCEPTED"
        reviewed_problem = use.status in {"HELD", "CONFLICT"} and use.review_context_id is not None
        pending = key in pending_keys
        if not (accepted or reviewed_problem or pending):
            continue
        if not final and accepted and use.fact_id not in pinned and use.query_id != query_id:
            if not (reader and state.active_view_ids is None) and (state.active_view_ids is None or use.fact_id not in state.active_view_ids):
                continue
        fact = state.facts[use.fact_id]
        refs.update(fact.source_refs)
        refs.update(use.reviewed_source_refs)
        node = {"node_id": key, **fact.model_dump(mode="json"), "query_id": use.query_id,
                "input_signature": use.input_signature, "use_status": use.status}
        if reviewed_problem:
            diagnostics.append({**node, "reason_code": use.reason_code})
        else:
            nodes.append(node)
    node_ids = {n["node_id"] for n in nodes}
    links = [link for link in state.navigation_links if link["to"] in node_ids and set(link["from"]) <= node_ids]
    if final:
        for qid, ex in state.executions.items():
            if ex.status != "RESOLVED":
                diagnostics.append({"query_id": qid, "status": ex.status,
                                    "review_barrier": ex.review_barrier})
    return EvidencePackV52(
        navigation={"facts": nodes, "links": links,
                    "binding_ports": [p.model_dump(mode="json") for p in state.binding_ports]},
        raw_evidence=archive.fetch_sentences(refs), unresolved_or_conflicts=diagnostics,
    )
