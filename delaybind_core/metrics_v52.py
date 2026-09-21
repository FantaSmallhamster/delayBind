"""Engineering coverage is not a measurement of semantic truth."""

from .navigation_graph import assert_dual_graph_invariants


def raw_first_metrics(state, pack, events):
    raw_refs = {r.source_ref for r in pack.raw_evidence} if pack else set()
    accepted = [u for u in state.uses.values() if u.status == "ACCEPTED" and
                u.input_signature == state.executions[u.query_id].input_signature]
    bindings = [b for b in state.binding_store.values() if b.valid]
    jobs = [j for j in state.recall_jobs.values() if j.stage != "INVALIDATED"]

    def rate(items, predicate):
        return sum(bool(predicate(item)) for item in items) / len(items) if items else None

    try:
        assert_dual_graph_invariants(state)
        consistent = 1.0
    except ValueError:
        consistent = 0.0
    assessments = [e for e in events if e["event_type"] in {"FACT_PROMOTED", "FACT_ASSESSED"}]
    accepted_refs = sorted({ref for u in accepted for ref in state.facts[u.fact_id].source_refs})
    return {
        "metrics_version": "v5.2",
        "anchor_valid_rate": rate(accepted_refs, lambda ref: ref in raw_refs),
        "anchor_valid_rate_scope": "current accepted fact references recoverable in final raw bundle",
        "raw_coverage_of_accepted_facts": rate(accepted, lambda u: set(state.facts[u.fact_id].source_refs) <= raw_refs),
        "raw_coverage_of_binding_support": rate(bindings, lambda b: set(b.source_refs) <= raw_refs),
        "recall_scan_completion": rate(jobs, lambda j: j.scan_offset == len(j.candidate_ids)),
        "source_review_acceptance": rate(assessments, lambda e: e["payload"]["verdict"] == "ACCEPT"),
        "source_semantic_accuracy": None,
        "dual_graph_consistency": consistent,
        "stale_binding_rate": rate(bindings, lambda b: state.executions[b.producer_query_id].current_binding_id != b.binding_id),
    }
