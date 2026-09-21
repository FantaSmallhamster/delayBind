"""Validate on a private state, commit SQL atomically, then publish both graphs."""

import json
import math

from .memory_protocol_v52 import Assess, Bind, Focus, MemoryProposal, Patch, Route, Unbind
from .navigation_graph import descendants, parent_bindings, query_by_id
from .schema_v52 import BindingRecord, FactUse, QueryExecutionV52, QueryPlanV3, digest, use_key
from .source_refs_v52 import SentenceRefResolver
from .subqueries_v52 import (
    add_queue, current_uses, invalidate, refresh_executions, schedule_recall, store_fact,
)


def _value_kind(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError("EMPTY_BINDING_VALUE")
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("NONFINITE_BINDING_VALUE")
        return "NUMBER"
    if isinstance(value, str):
        return "TEXT"
    if isinstance(value, list):
        for item in value:
            _value_kind(item)
        return "SET"
    if isinstance(value, dict) and value:
        for item in value.values():
            _value_kind(item)
        return "STRUCTURED"
    raise ValueError("INVALID_BINDING_VALUE")


def apply_memory_transaction(runtime, proposal: MemoryProposal, context):
    original = runtime.state
    if proposal.context_id != context.context_id:
        raise ValueError("CONTEXT_ID_MISMATCH")
    previous = runtime.store.connection.execute(
        "SELECT proposal_json FROM memory_transactions WHERE run_id=? AND context_id=?",
        (runtime.run_id, context.context_id)).fetchone()
    if previous and json.loads(previous[0]) == proposal.model_dump(mode="json"):
        return  # Idempotent delivery after a successful commit, including after restart.
    if context.state_revision != original.state_revision:
        raise ValueError("STALE_CONTEXT")
    # The caller must supply the actual snapshot recorded before model invocation.
    row = runtime.store.connection.execute(
        "SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
        (runtime.run_id, context.context_id)).fetchone()
    if row is None or json.loads(row[0])["context_hash"] != digest(context.model_dump(mode="json")):
        raise ValueError("UNREGISTERED_CONTEXT")
    visible = set(context.allowed_fact_ids)
    resolver = SentenceRefResolver(context.allowed_source_refs)
    for raw in context.working_memory.raw_evidence:
        if runtime.archive.fetch_sentence(raw.source_ref) != raw:
            raise ValueError("RAW_INTEGRITY_ERROR")
    assessed = {(op.query_id, op.fact_id) for op in proposal.operations if isinstance(op, Assess)}
    if not {(p["query_id"], p["fact_id"]) for p in context.pending_uses} <= assessed:
        raise ValueError("PENDING_USE_NOT_ASSESSED")
    staged = original.model_copy(deep=True)
    events, aliases, patched = [], {}, set()

    def visible_facts(ids):
        if not set(ids) <= visible:
            raise ValueError("FACT_NOT_VISIBLE")

    # PATCH is validated against the entire resulting DAG, independently of order.
    patches = [op for op in proposal.operations if isinstance(op, Patch)]
    if patches:
        queries = {q.id: q.model_dump() for q in staged.plan.queries}
        for op in patches:
            visible_facts(op.evidence_fact_ids)
            changes = op.patch.model_dump(exclude_unset=True)
            if not changes or any(value is None for value in changes.values()):
                raise ValueError("EMPTY_OR_NULL_PATCH")
            queries[op.query_id] = {**queries.get(op.query_id, {"id": op.query_id}), **changes}
            patched.add(op.query_id)
        new_plan = QueryPlanV3(plan_id=staged.plan.plan_id, queries=list(queries.values()))
        old_roots = patched & set(staged.executions)
        affected = invalidate(staged, old_roots, invalidate_root_uses=True)
        staged.plan = new_plan
        for qid in patched:
            if qid not in staged.executions:
                staged.executions[qid] = QueryExecutionV52()
            else:
                staged.executions[qid].version += 1
        affected |= descendants(staged, patched)
        if affected - patched:
            invalidate(staged, affected - patched, invalidate_root_uses=True)
        patched = affected
        refresh_executions(staged, reason="PLAN_CHANGED", callback=runtime.callback)
        events.append(("PLAN_CHANGED", {"affected_query_ids": sorted(patched)}))

    routed = set()
    for op in proposal.operations:
        if not isinstance(op, Route):
            continue
        visible_facts(op.fact_ids)
        if op.query_id not in staged.executions:
            raise ValueError("UNKNOWN_QUERY_ID")
        ex = staged.executions[op.query_id]
        for fid in op.fact_ids:
            bucket = staged.defer_workspace.setdefault(op.query_id, [])
            if fid not in bucket:
                bucket.append(fid)
            key = use_key(op.query_id, ex.input_signature, fid)
            if key not in staged.uses or staged.uses[key].status == "INVALIDATED":
                staged.uses[key] = FactUse(query_id=op.query_id, input_signature=ex.input_signature,
                                           fact_id=fid, status="CANDIDATE")
                routed.add(op.query_id)
        # ROUTE explicitly requests review, including in no-historical-callback mode.
        schedule_recall(staged, op.query_id, "ROUTE_CHANGED")
        events.append(("ROUTE_CHANGED", {"query_id": op.query_id, "fact_ids": op.fact_ids}))

    invalid_roots = set()
    for op in proposal.operations:
        if not isinstance(op, Assess):
            continue
        visible_facts([op.fact_id])
        if op.query_id in patched or op.query_id in routed:
            raise ValueError("CHANGED_QUERY_REQUIRES_NEW_REVIEW")
        if op.query_id not in context.input_signatures:
            raise ValueError("QUERY_USE_NOT_VISIBLE")
        ex = staged.executions[op.query_id]
        if ex.status == "DORMANT" or ex.input_signature != context.input_signatures[op.query_id]:
            raise ValueError("STALE_INPUT_SIGNATURE")
        key = use_key(op.query_id, ex.input_signature, op.fact_id)
        use = staged.uses.get(key)
        if use is None or use.status in {"CANDIDATE", "INVALIDATED"}:
            raise ValueError("USE_NOT_AUTHORIZED_FOR_REVIEW")
        resolver.resolve(op.checked_refs)
        fact = staged.facts[op.fact_id]
        if not set(fact.source_refs) <= set(op.checked_refs):
            raise ValueError("INCOMPLETE_REVIEWED_SOURCES")
        fid = op.fact_id
        if op.correction:
            resolver.resolve(op.correction.source_refs)
            if not set(op.correction.source_refs) <= set(op.checked_refs):
                raise ValueError("CORRECTION_NOT_REVIEWED")
            old_bases = {ref.split(":P")[0] for ref in fact.source_refs}
            if not old_bases & {ref.split(":P")[0] for ref in op.correction.source_refs}:
                raise ValueError("CORRECTION_DIFFERENT_SOURCE")
            fid = store_fact(staged, op.correction.text, op.correction.source_refs,
                             staged.read_watermark, supersedes=fact.fact_id)
            aliases[op.correction.local_id] = fid
            use.status = "INVALIDATED"
            staged.uses[use_key(op.query_id, ex.input_signature, fid)] = FactUse(
                query_id=op.query_id, input_signature=ex.input_signature, fact_id=fid, status="PENDING")
            staged.defer_workspace[op.query_id] = [f for f in staged.defer_workspace.get(op.query_id, []) if f != op.fact_id]
            staged.defer_workspace[op.query_id].append(fid)
            for other in staged.uses.values():
                if (other.fact_id == op.fact_id and other.query_id != op.query_id and
                    other.status == "ACCEPTED" and other.input_signature == staged.executions[other.query_id].input_signature):
                    other.status = "PENDING"
                    invalid_roots.add(other.query_id)
                    add_queue(staged, other.query_id)
            if ex.current_binding_id and op.fact_id in staged.binding_store[ex.current_binding_id].direct_fact_ids:
                invalid_roots.add(op.query_id)
            use = staged.uses[use_key(op.query_id, ex.input_signature, fid)]
            events.append(("FACT_CORRECTED", {"old_fact_id": op.fact_id, "fact_id": fid}))
        if op.verdict == "ACCEPT":
            if any(not runtime.archive.fetch_sentence(ref).complete for ref in staged.facts[fid].source_refs):
                raise ValueError("INCOMPLETE_SOURCE_REQUIRE_CORRECTION")
        status = {"ACCEPT": "ACCEPTED", "REJECT": "REJECTED", "HOLD": "HELD", "CONFLICT": "CONFLICT"}[op.verdict]
        use.status = status
        use.review_context_id = context.context_id
        use.reviewed_source_refs = sorted(set(op.checked_refs))
        use.reason_code = op.reason_code
        use.decision_revision = staged.state_revision + 1
        use.evidence_context_signature = digest([(r.source_ref, r.text_sha256) for r in context.working_memory.raw_evidence])
        if op.verdict in {"HOLD", "CONFLICT"} or (op.verdict == "REJECT" and ex.current_binding_id and
                op.fact_id in staged.binding_store[ex.current_binding_id].direct_fact_ids):
            if ex.current_binding_id:
                invalid_roots.add(op.query_id)
        if op.context_request:
            request = op.context_request
            resolver.resolve([request.anchor])
            for side in ("before", "after"):
                if getattr(request, side) > context.context_request_limits[side]:
                    raise ValueError("CONTEXT_REQUEST_LIMIT")
            staged.context_requests[key] = {**request.model_dump(), "query_id": op.query_id, "fact_id": fid}
        elif op.verdict != "HOLD":
            staged.context_requests.pop(key, None)
        events.append(("FACT_PROMOTED" if status == "ACCEPTED" else "FACT_ASSESSED", {
            "query_id": op.query_id, "fact_id": fid, "verdict": op.verdict,
            "reviewed_source_refs": use.reviewed_source_refs, "reason_code": op.reason_code,
            "context_id": context.context_id}))

    if invalid_roots:
        affected = invalidate(staged, invalid_roots)
        refresh_executions(staged, reason="SUPPORT_CHANGED", callback=runtime.callback)
        events.append(("BINDINGS_INVALIDATED", {"query_ids": sorted(affected)}))
        for qid in invalid_roots:
            if any(u.status == "PENDING" for u in current_uses(staged, qid)):
                add_queue(staged, qid)

    for op in proposal.operations:
        if isinstance(op, Unbind):
            visible_facts(op.evidence_fact_ids)
            if (op.query_id not in context.input_signatures or
                not original.executions[op.query_id].current_binding_id):
                raise ValueError("NO_CURRENT_BINDING")
            affected = invalidate(staged, {op.query_id})
            refresh_executions(staged, reason="UNBOUND", callback=runtime.callback)
            events.append(("BINDINGS_INVALIDATED", {"query_ids": sorted(affected), "reason": op.reason_code}))

    # Complete review jobs before testing BIND, enabling the final ASSESS+BIND batch.
    for job in staged.recall_jobs.values():
        if job.stage == "REVIEW":
            statuses = [staged.uses[use_key(job.query_id, job.input_signature, f)].status for f in job.selected_fact_ids]
            if all(status != "PENDING" for status in statuses):
                job.stage = "DONE"
    for qid, ex in staged.executions.items():
        ex.review_barrier = any(j.query_id == qid and j.stage in {"SCAN", "REVIEW", "SCAN_INCOMPLETE"}
                                for j in staged.recall_jobs.values())

    for op in proposal.operations:
        if not isinstance(op, Bind):
            continue
        qid = op.query_id
        if qid not in context.bindable_query_ids or qid in patched or qid in routed:
            raise ValueError("QUERY_NOT_BINDABLE")
        ex = staged.executions[qid]
        if ex.input_signature != context.input_signatures[qid] or ex.status == "DORMANT":
            raise ValueError("STALE_BIND_INPUT")
        if ex.review_barrier or any(u.status in {"PENDING", "HELD", "CONFLICT"} for u in current_uses(staged, qid)):
            raise ValueError("REVIEW_BARRIER_OPEN")
        query = query_by_id(staged, qid)
        if query.requires_complete_set and not staged.scope_closed:
            raise ValueError("SCOPE_NOT_CLOSED")
        kind = _value_kind(op.value)
        if (query.cardinality == "SET") != isinstance(op.value, list):
            raise ValueError("BINDING_CARDINALITY_MISMATCH")
        support = sorted(set(aliases.get(f, f) for f in op.support_fact_ids))
        if not set(support) <= (visible | set(aliases.values())):
            raise ValueError("SUPPORT_NOT_VISIBLE")
        parents = parent_bindings(staged, qid)
        if parents is None or (not support and (op.kind == "DIRECT" or not parents)):
            raise ValueError("MISSING_BINDING_PROOF")
        refs = set()
        for fid in support:
            use = staged.uses.get(use_key(qid, ex.input_signature, fid))
            if use is None or use.status != "ACCEPTED":
                raise ValueError("SUPPORT_NOT_ACCEPTED")
            refs.update(staged.facts[fid].source_refs)
        for parent in parents:
            refs.update(parent.source_refs)
        resolver.resolve(sorted(refs))
        old = staged.binding_store.get(ex.current_binding_id)
        if old and digest(old.value) == digest(op.value) and old.direct_fact_ids == support and old.kind == op.kind:
            continue
        revision = max((b.binding_revision for b in staged.binding_store.values() if b.producer_query_id == qid), default=0) + 1
        invalidate(staged, {qid}, preserve_root_jobs=True)
        bid = f"B{len(staged.binding_store) + 1}"
        binding = BindingRecord(
            binding_id=bid, producer_query_id=qid, variable=query.output, value=op.value, value_kind=kind,
            kind=op.kind, query_version=ex.version, input_signature=ex.input_signature,
            binding_revision=revision, direct_fact_ids=support, parent_binding_ids=[p.binding_id for p in parents],
            source_refs=sorted(refs), created_from_context=context.context_id)
        staged.binding_store[bid] = binding
        ex.current_binding_id = bid
        ex.status = "RESOLVED"
        refresh_executions(staged, trigger=bid, callback=runtime.callback)
        events.append(("BINDING_CREATED", binding.model_dump(mode="json")))

    for op in proposal.operations:
        if isinstance(op, Focus):
            visible_facts(op.fact_ids)
            accepted = {u.fact_id for u in staged.uses.values() if u.status == "ACCEPTED" and
                        u.input_signature == staged.executions[u.query_id].input_signature}
            if not set(op.fact_ids) <= accepted:
                raise ValueError("FOCUS_NOT_ACCEPTED")
            staged.active_view_ids = sorted(set(op.fact_ids))
    # Consume the current view; a new version/job or remaining batch can requeue it.
    for qid in context.input_signatures:
        if qid not in patched and qid not in routed and not any(u.status == "PENDING" for u in current_uses(staged, qid)):
            staged.review_queue = [q for q in staged.review_queue if q != qid]
    runtime.commit(staged, events, context_id=context.context_id, proposal=proposal.model_dump(mode="json"))
