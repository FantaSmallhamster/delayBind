"""Stage evidence overlays; publish one BIND/REBIND decision atomically."""

from .schema_v52 import FactNode, digest
from .schema_r2 import ReviewRecord, BindingR2, fact_id, use_key
from .navigation_r2 import (query, query_ready, current_uses, proof_refs, refresh, binding_effective, descendants)
from .review_jobs_r2 import ensure_use, ensure_review, schedule_ready, event, evidence_signature, enqueue
from .runtime_r2 import invalidate_closure
from .source_refs_v52 import SentenceRefResolver


def apply_memory(runtime, response, ctx):
    encoded = response.model_dump(mode="json")
    receipt = runtime.store.r2_receipt(runtime.run_id, ctx.context_id, digest(encoded))
    if receipt:
        return receipt
    s0 = runtime.state
    r0 = s0.reviews.get(ctx.review_id)
    ex = s0.executions[ctx.query_id]
    if (response.context_id != ctx.context_id or response.review_id != ctx.review_id
            or ctx.state_revision != s0.state_revision or r0 is None or r0.status in {"DONE", "CANCELLED"}
            or ex.version != ctx.query_version or ex.input_signature != ctx.input_signature
            or ex.current_binding_id != ctx.expected_binding_id or ex.evidence_revision != ctx.inbox_revision
            or ctx.candidate_bucket_version != digest(sorted(s0.route_index.get(ctx.query_id, [])))
            or not query_ready(s0, ctx.query_id)):
        raise ValueError("STALE_MEMORY_CONTEXT")
    manifest = runtime.store.connection.execute("SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
                                                 (runtime.run_id, ctx.context_id)).fetchone()
    import json
    if not manifest or json.loads(manifest[0]) != {"interface": "MEMORY_SNAPSHOT", **ctx.model_dump(mode="json")}:
        raise ValueError("MEMORY_CONTEXT_NOT_REGISTERED")
    action = response.operations[0]
    if action.op != ctx.allowed_mode or action.query_id != ctx.query_id:
        raise ValueError("MEMORY_MODE_OR_QUERY_MISMATCH")
    if (ctx.phase == "REVIEW") != (action.binding_result is None):
        raise ValueError("MEMORY_PHASE_RESULT_MISMATCH")
    ids = [r.fact_id for r in action.evidence_reviews]
    if ctx.fact_only and ids:
        raise ValueError("FACT_ONLY_MEMORY_HAS_RAW_REVIEWS")
    if (not ctx.fact_only and (len(ids) != len(set(ids)) or not set(ctx.required_reviews) <= set(ids)
                               or not set(ids) <= set(ctx.allowed_review_ids))):
        raise ValueError("REQUIRED_REVIEWS_OR_PERMISSION_MISMATCH")
    resolver = None if ctx.fact_only else SentenceRefResolver(ctx.visible_source_refs)
    if not ctx.fact_only:
        for ref, expected in ctx.raw_hashes.items():
            if runtime.archive.fetch_sentence(ref).text_sha256 != expected:
                raise ValueError("RAW_INTEGRITY_ERROR")
    s, events, aliases = s0.model_copy(deep=True), [], {}
    if response.ignored_lines:
        event(events, "MEMORY_LINES_IGNORED", query_id=ctx.query_id, review_id=ctx.review_id,
              phase=ctx.phase, ignored_lines=response.ignored_lines)
    ready_before = {q.id: query_ready(s0, q.id) for q in s0.plan.queries}
    signatures_before = {qid: ex.input_signature for qid, ex in s0.executions.items()}
    session = s.reviews[ctx.review_id]
    session.sent = True
    if ctx.context_id not in session.context_ids:
        session.context_ids.append(ctx.context_id)
    session.phase = ctx.phase
    for review in action.evidence_reviews:
        resolver.resolve(review.checked_refs)
        fid = review.fact_id
        key = use_key(ctx.query_id, ctx.query_version, ctx.input_signature, fid)
        if key not in s.uses or s.uses[key].status in {"CANDIDATE", "INVALIDATED"}:
            raise ValueError("USE_NOT_AUTHORIZED_FOR_REVIEW")
        if not set(s.facts[fid].source_refs) <= set(review.checked_refs):
            raise ValueError("INCOMPLETE_REVIEWED_SOURCES")
        corrected = None
        if review.correction:
            c = review.correction
            resolver.resolve(c.source_refs)
            if c.local_id in aliases or not set(c.source_refs) <= set(review.checked_refs):
                raise ValueError("INVALID_CORRECTION")
            if not set(c.source_refs) & set(s.facts[fid].source_refs) and not any(
                    ref.split(":P")[0] in c.source_refs for ref in s.facts[fid].source_refs):
                raise ValueError("CORRECTION_UNRELATED_SOURCE")
            corrected = fact_id(c.text, c.source_refs)
            aliases[c.local_id] = corrected
            if corrected not in s.facts:
                s.facts[corrected] = FactNode(fact_id=corrected, text=c.text, source_refs=tuple(sorted(c.source_refs)),
                                             observed_window=s.read_watermark, supersedes_fact_id=fid)
        checked_fact = s.facts[corrected or fid]
        if review.verdict == "ACCEPT" and any(not runtime.archive.fetch_sentence(ref).complete for ref in checked_fact.source_refs):
            raise ValueError("INCOMPLETE_SOURCE_REQUIRE_CORRECTION")
        if review.context_request:
            req = review.context_request
            resolver.resolve([req.anchor])
            if req.before > ctx.context_limits["before"] or req.after > ctx.context_limits["after"]:
                raise ValueError("CONTEXT_RANGE_EXCEEDED")
            session.context_requests[key] = req
        else:
            session.context_requests.pop(key, None)
        record_hash = digest([key, review.model_dump(), ctx.raw_hashes, ctx.context_id])
        rid = "ER" + record_hash
        s.review_records[rid] = ReviewRecord(record_id=rid, use_key=key, review=review, context_id=ctx.context_id,
                                              record_hash=record_hash, raw_hashes=ctx.raw_hashes, corrected_fact_id=corrected)
        session.staged_review_ids[key] = rid
        event(events, "EVIDENCE_REVIEW_STAGED", query_id=ctx.query_id, review_id=session.review_id,
              record_id=rid, mode=session.mode, input_signature=ctx.input_signature)
    if ctx.phase == "REVIEW":
        reviews_complete = set(session.required_use_ids) <= set(session.staged_review_ids)
        wait_for_scope = (not ctx.member_bindings and reviews_complete and query(s, ctx.query_id).requires_complete_set
                          and not s.scope_closed and not session.context_requests)
        for j in s.jobs.values():
            if j.review_id == session.review_id and j.kind == "MEMORY" and j.status not in {"DONE", "CANCELLED"}:
                j.status, j.lease = ("WAITING_SCOPE" if wait_for_scope else "PENDING"), None
        if wait_for_scope:
            session.status = "WAITING_SCOPE"
            event(events, "COLLECTION_FINALIZATION_DEFERRED", query_id=ctx.query_id,
                  review_id=session.review_id, caused_by="SCOPE_OPEN")
        if session.context_requests:
            enqueue(s, ctx.query_id, "CONTEXT_EXPAND", "HOLD_CONTEXT_REQUESTED", review_id=session.review_id,
                    payload={"watermark": s.read_watermark, "records": sorted(session.staged_review_ids.values())}, events=events)
        return runtime.commit(s, events, context_id=ctx.context_id, response=encoded, receipt={"changed": False, "review_complete": False})
    result = action.binding_result
    if (not ctx.barriers["can_finalize"]
            or (not ctx.fact_only and set(session.required_use_ids) - set(session.staged_review_ids))):
        raise ValueError("REVIEW_OR_SCAN_INCOMPLETE")
    published_members = []
    if ctx.member_bindings:
        from .member_graph_r2 import stage_branch
        result, published_members = stage_branch(s, ctx, result, session, events)
        if result is None:
            return runtime.commit(s, events, context_id=ctx.context_id, response=encoded,
                                  receipt={"changed": False, "review_complete": False, "branch_staged": True})
    if result.decision_source_refs and resolver is not None:
        resolver.resolve(result.decision_source_refs)
    if not ctx.fact_only and not ctx.member_bindings and result.state == "NOOP":
        raise ValueError("NOOP_ONLY_ALLOWED_IN_FACT_ONLY_MODE")
    # Finalize overlay on the clone only. Any validation below rolls everything back.
    corrected_notifications = []
    for key, record_id in session.staged_review_ids.items():
        record = s.review_records[record_id]
        review = record.review
        u = s.uses[key]
        target = u
        if record.corrected_fact_id and record.corrected_fact_id != u.fact_id:
            u.status = "REJECTED"
            target = ensure_use(s, ctx.query_id, record.corrected_fact_id, status="PENDING", origin=u.acceptance_origin)
            bucket = s.route_index.setdefault(ctx.query_id, [])
            if record.corrected_fact_id not in bucket:
                bucket.append(record.corrected_fact_id)
                bucket.sort()
            corrected_notifications.append((u.fact_id, record.corrected_fact_id))
            event(events, "FACT_CORRECTED", old_fact_id=u.fact_id, fact_id=record.corrected_fact_id, query_id=ctx.query_id)
        target.status = {"ACCEPT": "ACCEPTED", "REJECT": "REJECTED", "HOLD": "HELD", "CONFLICT": "CONFLICT"}[review.verdict]
        target.reviewed_source_refs = list(review.checked_refs)
        target.review_context_id = record.context_id
        target.decision_revision = s.state_revision + 1
        target.reason_code = review.reason_code
        target.evidence_context_signature = digest(record.raw_hashes)
        event(events, "FACT_USE_" + target.status, use_id=target.use_id, query_id=ctx.query_id, review_id=session.review_id)
    support = sorted(set(aliases.get(fid, fid) for fid in result.support_fact_ids))
    fact_only_noop = ctx.fact_only and result.state in {"NOOP", "UNBOUND"}
    if ctx.fact_only:
        if result.decision_source_refs:
            raise ValueError("FACT_ONLY_RESULT_HAS_SOURCE_REFS")
        allowed = set(ctx.allowed_fact_ids)
        current = current_uses(s, ctx.query_id)
        current_by_fact = {u.fact_id: u for u in current}
        if not set(support) <= allowed or not set(support) <= set(current_by_fact):
            raise ValueError("SUPPORT_NOT_VISIBLE")
        if fact_only_noop:
            # NOOP is deliberately non-destructive. Candidates remain available
            # for a later evidence revision and an existing proof stays intact.
            event(events, "FACT_ONLY_DECISION_NOOP", query_id=ctx.query_id,
                  review_id=session.review_id, preserved_binding_id=session.target_binding_id)
        else:
            # Only selected facts become the effective proof. Non-selected facts
            # remain candidates; lack of selection is not evidence of falsity.
            for use in current:
                use.status = "ACCEPTED" if use.fact_id in support else "CANDIDATE"
                use.reviewed_source_refs = []
                use.review_context_id = ctx.context_id
                use.decision_revision = s.state_revision + 1
                use.reason_code = "FACT_SELECTED" if use.fact_id in support else "FACT_NOT_SELECTED"
                use.evidence_context_signature = digest([])
                event(events, "FACT_USE_" + use.status, use_id=use.use_id, query_id=ctx.query_id,
                      review_id=session.review_id, fact_only=True)
            event(events, "FACT_ONLY_DECISION_FINALIZED", query_id=ctx.query_id,
                  review_id=session.review_id, support_fact_ids=support)
    event(events, "EVIDENCE_REVIEWS_FINALIZED", query_id=ctx.query_id, review_id=session.review_id,
          record_ids=sorted(session.staged_review_ids.values()))
    parent_ids = [s.executions[p].current_binding_id for p in query(s, ctx.query_id).depends_on]
    use_ids, raw_refs = [], set()
    if result.state == "BOUND":
        if not ctx.member_bindings and any(u.status in {"PENDING", "HELD", "CONFLICT"} for u in current_uses(s, ctx.query_id)):
            raise ValueError("UNRESOLVED_REVIEW_BLOCKS_BOUND")
        q = query(s, ctx.query_id)
        if not ctx.member_bindings and q.requires_complete_set and not s.scope_closed:
            raise ValueError("COLLECTION_SCOPE_OPEN")
        if not ctx.member_bindings and (q.cardinality == "SET") != isinstance(result.value, list):
            raise ValueError("BINDING_CARDINALITY_MISMATCH")
        if not support and (result.kind == "DIRECT" or not parent_ids):
            raise ValueError("MISSING_BINDING_PROOF")
        if result.kind == "INFERRED" and not any(ctx.query_id in child.depends_on for child in s.plan.queries):
            raise ValueError("INFERRED_ONLY_FOR_INTERMEDIATE_QUERY")
        if result.value == [] and (not s.scope_closed or not support):
            raise ValueError("EMPTY_SET_NOT_PROVEN")
        for fid in support:
            if fid not in set(ctx.allowed_fact_ids) | set(aliases.values()):
                raise ValueError("SUPPORT_NOT_VISIBLE")
            key = use_key(ctx.query_id, ctx.query_version, ctx.input_signature, fid)
            if key not in s.uses or s.uses[key].status != "ACCEPTED":
                raise ValueError("SUPPORT_NOT_ACCEPTED")
            if resolver is not None:
                resolver.resolve(s.facts[fid].source_refs)
            raw_refs.update(s.facts[fid].source_refs)
            use_ids.append(key)
        for bid in parent_ids:
            if not binding_effective(s, bid):
                raise ValueError("PARENT_NOT_EFFECTIVE")
            if resolver is not None:
                resolver.resolve(s.binding_store[bid].source_refs)
            raw_refs.update(s.binding_store[bid].source_refs)
    old_id = session.target_binding_id
    old = s.binding_store.get(old_id)
    normalized_value = [v for _, v in sorted({digest(v): v for v in result.value}.items())] if isinstance(result.value, list) else result.value
    unchanged = bool(old and result.state == "BOUND" and digest(old.value) == digest(normalized_value)
                     and old.direct_use_ids == sorted(use_ids) and old.parent_binding_ids == sorted(parent_ids)
                     and old.kind == result.kind and (not ctx.member_bindings or old.members == published_members))
    affected = set()
    if old and not unchanged and not fact_only_noop:
        affected = invalidate_closure(s, {ctx.query_id}, events, preserve_root_uses=True, except_review=session.review_id)
    if unchanged:
        event(events, "BINDING_REAFFIRMED", query_id=ctx.query_id, binding_id=old_id, review_id=session.review_id)
    elif result.state == "BOUND":
        revision = 1 + max((b.binding_revision for b in s.binding_store.values() if b.producer_query_id == ctx.query_id), default=0)
        bid = "B" + str(len(s.binding_store) + 1)
        s.binding_store[bid] = BindingR2(binding_id=bid, producer_query_id=ctx.query_id, variable=query(s, ctx.query_id).output,
            query_version=ctx.query_version, input_signature=ctx.input_signature, value=normalized_value, kind=result.kind,
            binding_revision=revision, parent_binding_ids=sorted(parent_ids), direct_use_ids=sorted(use_ids), direct_fact_ids=support,
            source_refs=sorted(raw_refs), decision_source_refs=sorted(set(result.decision_source_refs)),
            supersedes_binding_id=old_id, created_from_context=ctx.context_id, members=published_members)
        s.executions[ctx.query_id].current_binding_id = bid
        event(events, "BINDING_CREATED", query_id=ctx.query_id, binding_id=bid, mode=session.mode)
    else:
        diagnostic_state = "NOOP" if fact_only_noop else "UNBOUND"
        s.diagnostics.append(dict(query_id=ctx.query_id, state=diagnostic_state, reason_code=result.reason_code,
                                  source_refs=result.decision_source_refs, review_id=session.review_id,
                                  query_version=ctx.query_version, input_signature=ctx.input_signature))
    session.status = "DONE"
    session.completed_revision = s.state_revision + 1
    ex = s.executions[ctx.query_id]
    if session.review_id in ex.blocking_review_ids:
        ex.blocking_review_ids.remove(session.review_id)
        event(events, "REVIEW_BARRIER_REMOVED", query_id=ctx.query_id, review_id=session.review_id)
    ex.last_decision_signature = evidence_signature(s, ctx.query_id)
    ex.retry_gate = ex.last_decision_signature
    for j in s.jobs.values():
        if j.review_id == session.review_id and j.status != "CANCELLED":
            j.status, j.lease = "DONE", None
            event(events, "JOB_COMPLETED", job_id=j.job_id)
    for entry in s.inbox.values():
        if entry.review_id == session.review_id:
            entry.status = "DONE"
    event(events, "REVIEW_COMPLETED", query_id=ctx.query_id, review_id=session.review_id, mode=session.mode)
    refresh(s)
    for qid, previous in signatures_before.items():
        if previous != s.executions[qid].input_signature:
            event(events, "QUERY_INSTANCE_CHANGED", query_id=qid, old_input_signature=previous,
                  input_signature=s.executions[qid].input_signature)
    if not unchanged and result.state == "BOUND":
        for child in s.plan.queries:
            if ctx.query_id in child.depends_on:
                if not ready_before[child.id] and query_ready(s, child.id):
                    event(events, "QUERY_ACTIVATED", query_id=child.id, producer_query_id=ctx.query_id,
                          binding_id=s.executions[ctx.query_id].current_binding_id,
                          input_signature=s.executions[child.id].input_signature)
                schedule_ready(s, child.id, "UPSTREAM_REBOUND" if old else "UPSTREAM_BOUND", events,
                               callback=runtime.config.enable_defer_callback)
    # Resume saved child inboxes after a reaffirm without recomputing valid chains.
    if unchanged or (fact_only_noop and old is not None):
        for child in s.plan.queries:
            if any(u.status == "PENDING" for u in current_uses(s, child.id)) and query_ready(s, child.id):
                ensure_review(s, child.id, "UPSTREAM_REAFFIRMED", events)
            elif not ready_before[child.id] and query_ready(s, child.id) and not s.executions[child.id].current_binding_id:
                schedule_ready(s, child.id, "UPSTREAM_REAFFIRMED", events, callback=runtime.config.enable_defer_callback)
    for old_fid, new_fid in corrected_notifications:
        for qid, bucket in s.route_index.items():
            notification = digest([old_fid, new_fid, qid])
            if qid == ctx.query_id or old_fid not in bucket or notification in s.correction_notifications:
                continue
            s.correction_notifications.append(notification)
            if new_fid not in bucket:
                bucket.append(new_fid)
                bucket.sort()
            s.executions[qid].evidence_revision += 1
            if query_ready(s, qid):
                ensure_use(s, qid, new_fid, status="PENDING", origin="CORRECTION")
                # Existing use must be reviewed too; do not silently replace it.
                old_use = ensure_use(s, qid, old_fid, status="PENDING")
                current_bid = s.executions[qid].current_binding_id
                if not current_bid or old_use.use_id not in s.binding_store[current_bid].direct_use_ids:
                    old_use.status = "PENDING"
                ensure_review(s, qid, "FACT_CORRECTED", events)
            else:
                ensure_use(s, qid, new_fid, status="CANDIDATE", origin="CORRECTION")
    event(events, "NAVIGATION_REBUILT", query_id=ctx.query_id, affected_query_ids=sorted(affected))
    changed = ((result.state == "BOUND" and not unchanged)
               or (result.state == "UNBOUND" and old is not None and not ctx.fact_only))
    return runtime.commit(s, events, context_id=ctx.context_id, response=encoded,
                          receipt=dict(changed=changed,
                                       affected_query_ids=sorted(affected), review_complete=True,
                                       retired_binding_ids=[p["binding_id"] for e, p in events if e == "BINDING_RETIRED"],
                                       created_binding_ids=[p["binding_id"] for e, p in events if e == "BINDING_CREATED"],
                                       enqueued_job_ids=[p["job_id"] for e, p in events if e == "JOB_ENQUEUED"],
                                       cancelled_job_ids=[p["job_id"] for e, p in events if e == "JOB_CANCELLED"]))
