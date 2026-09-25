"""Structural R2 diagnostics; never conflate protocol validity with entailment."""
from collections import Counter


def r2_metrics(state, pack, events, requests, model_calls=()):
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
    answers = [r for r in requests if r["interface"] == "ANSWER"]
    evaluated = [e["payload"] for e in events if e["event_type"] == "MEMORY_RESULT_EVALUATED"]
    applied = [e["payload"] for e in events if e["event_type"] == "UNIFIED_MEMORY_RESULT_APPLIED"]
    transport = [call for call in model_calls if call.interface in {"MEMORY", "MEMORY_REPAIR"}
                 and not call.cache_hit]
    # Count every provider attempt, including rejected truncated responses.
    # Offline clients have no transport records; their manifests carry counts.
    memory_input_tokens = (sum(call.input_tokens or 0 for call in transport) if transport else
                           sum(r.get("memory_input_tokens", r.get("input_tokens", 0)) for r in reviewed))
    memory_output_tokens = (sum(call.output_tokens or 0 for call in transport) if transport else
                            sum(r.get("memory_output_tokens", 0) for r in reviewed))
    token_source = ("provider" if all(call.input_tokens is not None and call.output_tokens is not None
                                     for call in transport) else "provider_partial") if transport else "local_tokenizer"
    return dict(memory_mode_phase=phases, bind_requests=kinds["BIND_REVIEW_REQUESTED"],
                rebind_requests=kinds["REBIND_REVIEW_REQUESTED"], reaffirmations=kinds["BINDING_REAFFIRMED"],
                bindings_created=kinds["BINDING_CREATED"], bindings_retired=kinds["BINDING_RETIRED"],
                stale_results_rejected=kinds["STALE_RESULT_REJECTED"],
                review_barriers=sum(len(e.blocking_review_ids) for e in state.executions.values()),
                complete_bucket_scan_rate=sum(j.status == "DONE" for j in recall) / len(recall) if recall else 1.0,
                protocol_repairs=sum(r["interface"] == "MEMORY_REPAIR" for r in reviewed),
                verify_calls=sum(r["interface"] == "VERIFY" for r in requests),
                answer_raw_count=len(pack.raw_evidence) if pack else 0,
                answer_agent_role=answers[-1].get("agent_role") if answers else None,
                answer_model=answers[-1].get("client_model") if answers else None,
                recall_candidates=sum(len(j.payload.get("candidates", [])) for j in recall),
                recall_selected=sum(len(j.payload.get("selected", [])) for j in recall),
                memory_skipped_no_evidence=kinds["MEMORY_SKIPPED"],
                authorized_facts_per_memory=[r.get("authorized_fact_count") for r in reviewed],
                update_admissions_per_memory=[r.get("update_admission_count") for r in reviewed],
                recall_admissions_per_memory=[r.get("recall_admission_count") for r in reviewed],
                prior_support_per_memory=[r.get("prior_support_count") for r in reviewed],
                memory_interface=state.memory_interface,
                memory_interface_version=("query-facts-result-v1" if state.memory_interface == "unified_evidence_v1" else None),
                recall_model_calls=sum(r["interface"] == "RECALL" for r in requests),
                evidence_origins_per_memory=[r.get("evidence_origins", []) for r in reviewed],
                evidence_facts_per_memory=[r.get("evidence_fact_count") for r in reviewed],
                rendered_facts_per_memory=[r.get("rendered_fact_count") for r in reviewed],
                support_fact_count=sum(e.get("support_fact_count", 0) for e in evaluated),
                supported_member_count=sum(e.get("supported_member_count", 0) for e in evaluated),
                unknown_result_count=sum(bool(e.get("unknown_result")) for e in evaluated),
                unchanged_result_count=sum(bool(e.get("unchanged")) for e in applied),
                binding_changed_count=sum(bool(e.get("binding_changed")) for e in applied),
                memory_input_tokens=memory_input_tokens,
                memory_output_tokens=memory_output_tokens,
                memory_token_usage_source=token_source,
                semantic_support_accuracy=None)
