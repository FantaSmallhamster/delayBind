"""Structural R2 diagnostics; never conflate protocol validity with entailment."""
from collections import Counter


def r2_metrics(state, pack, events, requests):
    kinds = Counter(e["event_type"] for e in events)
    reviewed = [r for r in requests if r["interface"] in {"MEMORY", "MEMORY_REPAIR"}]
    phases = {}
    for r in reviewed:
        key = str(r.get("memory_mode")) + "/" + str(r.get("review_phase"))
        data = phases.setdefault(key, dict(calls=0, estimated_input_tokens=0, raw_input_tokens=0))
        data["calls"] += 1
        data["estimated_input_tokens"] += r.get("input_tokens", 0)
        data["raw_input_tokens"] += r.get("raw_input_tokens", 0)
    recall = [j for j in state.jobs.values() if j.kind == "RECALL" and j.status != "CANCELLED"]
    return dict(memory_mode_phase=phases, bind_requests=kinds["BIND_REVIEW_REQUESTED"],
                rebind_requests=kinds["REBIND_REVIEW_REQUESTED"], reaffirmations=kinds["BINDING_REAFFIRMED"],
                bindings_created=kinds["BINDING_CREATED"], bindings_retired=kinds["BINDING_RETIRED"],
                stale_results_rejected=kinds["STALE_RESULT_REJECTED"],
                review_barriers=sum(len(e.blocking_review_ids) for e in state.executions.values()),
                complete_bucket_scan_rate=sum(j.status == "DONE" for j in recall) / len(recall) if recall else 1.0,
                protocol_repairs=sum(r["interface"] == "MEMORY_REPAIR" for r in reviewed),
                verify_calls=sum(r["interface"] == "VERIFY" for r in requests),
                answer_raw_count=len(pack.raw_evidence) if pack else 0,
                semantic_support_accuracy=None)
