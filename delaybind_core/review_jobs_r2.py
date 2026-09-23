"""Pure staged scheduling: deduplication, readiness, review barriers and outbox."""

from .schema_v52 import digest
from .schema_r2 import DurableJob, ReviewSession, FactUseR2, use_key
from .navigation_r2 import query, current_uses, query_ready, topological, refresh
from .memory_admission_r2 import (strict, build_admission_snapshot, pending_admissions,
                                  memory_work_decision)

TERMINAL = {"DONE", "CANCELLED"}


def event(events, event_type, **payload):
    events.append((event_type, payload))


def evidence_signature(state, qid):
    ex = state.executions[qid]
    return digest([ex.version, ex.input_signature, ex.evidence_revision, state.scope_closed,
                   sorted(state.route_index.get(qid, []))])


def enqueue(state, qid, kind, trigger, *, review_id=None, payload=None, events):
    ex = state.executions[qid]
    bucket = digest(sorted(state.route_index.get(qid, [])))
    key = digest([kind, qid, ex.version, ex.input_signature, ex.current_binding_id,
                  ex.evidence_revision, bucket, review_id, payload or {}])
    existing = next((j for j in state.jobs.values() if j.job_key == key), None)
    if existing:
        return existing
    jid = "J" + key
    job = DurableJob(job_id=jid, job_key=key, kind=kind, target_query=qid, query_version=ex.version,
                     input_signature=ex.input_signature, review_id=review_id, bucket_version=bucket,
                     generation=ex.evidence_revision, trigger_event=trigger, payload=payload or {})
    state.jobs[jid] = job
    event(events, "JOB_ENQUEUED", job_id=jid, query_id=qid, kind=kind, review_id=review_id, caused_by=trigger)
    return job


def ensure_use(state, qid, fid, *, status, origin="UPDATE"):
    ex = state.executions[qid]
    key = use_key(qid, ex.version, ex.input_signature, fid)
    if key not in state.uses:
        state.uses[key] = FactUseR2(use_id=key, query_id=qid, query_version=ex.version,
                                  input_signature=ex.input_signature, fact_id=fid, status=status, acceptance_origin=origin)
    return state.uses[key]


def cancel_reviews(state, qids, events):
    for r in state.reviews.values():
        if r.query_id in qids and r.status not in TERMINAL:
            r.status = "CANCELLED"
            ex = state.executions[r.query_id]
            if r.review_id in ex.blocking_review_ids:
                ex.blocking_review_ids.remove(r.review_id)
                event(events, "REVIEW_BARRIER_REMOVED", query_id=r.query_id, review_id=r.review_id, caused_by="CONTEXT_INVALIDATED")
    for j in state.jobs.values():
        if j.target_query in qids and j.status not in TERMINAL:
            j.status, j.lease = "CANCELLED", None
            event(events, "JOB_CANCELLED", job_id=j.job_id, query_id=j.target_query)


def ensure_review(state, qid, trigger, events, *, enqueue_memory=True):
    ex = state.executions[qid]
    if not query_ready(state, qid):
        return None
    signature = evidence_signature(state, qid)
    if ex.retry_gate == signature:
        return None
    uses = current_uses(state, qid)
    snapshot = build_admission_snapshot(state, qid) if strict(state) else None
    required = (set(snapshot["use_ids"]) if snapshot else
                {u.use_id for u in uses if u.status in {"PENDING", "HELD", "CONFLICT"}})
    if ex.current_binding_id and not snapshot:
        required.update(state.binding_store[ex.current_binding_id].direct_use_ids)
    active = next((r for r in state.reviews.values() if r.query_id == qid and r.status not in TERMINAL), None)
    if active and active.evidence_signature == signature:
        if enqueue_memory and active.status == "WAITING_RECALL":
            active.status = "PENDING"
            enqueue(state, qid, "MEMORY", trigger, review_id=active.review_id, events=events)
        return active
    if active:
        cancel_reviews(state, {qid}, events)
    mode = "REBIND" if ex.current_binding_id else "BIND"
    rid = "R" + digest([qid, ex.version, ex.input_signature, ex.current_binding_id, signature])
    # Retry gates prevent reusing an already decided session with the same evidence.
    if rid in state.reviews and state.reviews[rid].status == "DONE":
        return state.reviews[rid]
    r = ReviewSession(review_id=rid, query_id=qid, mode=mode, query_version=ex.version,
                      input_signature=ex.input_signature, target_binding_id=ex.current_binding_id,
                      inbox_revision=ex.evidence_revision, candidate_bucket_version=digest(sorted(state.route_index.get(qid, []))),
                      evidence_signature=signature, required_use_ids=sorted(required), trigger=trigger,
                      admitted_use_tokens=snapshot["admitted_use_tokens"] if snapshot else {},
                      prior_support_use_ids=snapshot["prior_support_use_ids"] if snapshot else [])
    from .member_graph_r2 import enabled, branches_for
    if enabled(state):
        r.branches = branches_for(state, qid)
        if not r.branches:
            return None
    state.reviews[rid] = r
    if mode == "REBIND":
        ex.blocking_review_ids.append(rid)
        event(events, "REVIEW_BARRIER_ADDED", query_id=qid, review_id=rid, mode=mode)
    for entry in state.inbox.values():
        if entry.query_id == qid and entry.status == "PENDING":
            entry.review_id = rid
    if not enqueue_memory:
        r.status = "WAITING_RECALL"
        return r
    job = enqueue(state, qid, "MEMORY", trigger, review_id=rid, events=events)
    # Fact-only MEMORY has no intermediate raw-review call. Complete-set
    # queries therefore wait for EOF without spending a model call.
    if not enabled(state) and state.fact_only and query(state, qid).requires_complete_set and not state.scope_closed:
        r.status = "WAITING_SCOPE"
        job.status = "WAITING_SCOPE"
        event(events, "COLLECTION_FINALIZATION_DEFERRED", query_id=qid,
              review_id=rid, caused_by="SCOPE_OPEN")
    event(events, mode + "_REVIEW_REQUESTED", query_id=qid, review_id=rid, mode=mode,
          input_signature=ex.input_signature, caused_by=trigger)
    return r


def schedule_ready(state, qid, trigger, events, *, callback=True, force_review=False):
    if not query_ready(state, qid):
        return
    ex = state.executions[qid]
    if strict(state):
        if ex.retry_gate == evidence_signature(state, qid):
            return
        pending = pending_admissions(state, qid)
        support = set(build_admission_snapshot(state, qid)["prior_support_use_ids"])
        candidates = sorted(fid for fid in state.route_index.get(qid, []) if
                            not any(u.fact_id == fid and (u.use_id in pending or u.use_id in support)
                                    for u in current_uses(state, qid)))
        active_scan = any(j.target_query == qid and j.kind == "RECALL" and j.status not in TERMINAL
                          for j in state.jobs.values())
        if active_scan:
            return
        if candidates and callback:
            for fid in candidates:
                ensure_use(state, qid, fid, status="CANDIDATE", origin="RECALL")
            if ex.current_binding_id and (pending or force_review):
                ensure_review(state, qid, trigger, events, enqueue_memory=False)
            enqueue(state, qid, "RECALL", trigger,
                    payload={"candidates": candidates, "selected": [], "offset": 0}, events=events)
            return
        finish_strict_work(state, qid, trigger, events, force_review=force_review)
        return
    candidates = sorted(fid for fid in state.route_index.get(qid, []) if
                        not any(u.fact_id == fid and u.status not in {"CANDIDATE", "INVALIDATED"} for u in current_uses(state, qid)))
    if candidates and callback:
        for fid in candidates:
            ensure_use(state, qid, fid, status="CANDIDATE", origin="RECALL")
        enqueue(state, qid, "RECALL", trigger, payload={"candidates": candidates, "selected": [], "offset": 0}, events=events)
    elif force_review or any(u.status == "PENDING" for u in current_uses(state, qid)) or query(state, qid).inputs:
        ensure_review(state, qid, trigger, events)


def finish_strict_work(state, qid, trigger, events, *, force_review=False):
    """Decide after recall has finished; never rescan the same bucket here."""
    decision = memory_work_decision(state, qid)
    if decision == "CALL" and (force_review or pending_admissions(state, qid)
                               or any(r.query_id == qid and r.status == "WAITING_RECALL" for r in state.reviews.values())
                               or state.executions[qid].current_binding_id or
                               query(state, qid).allow_upstream_only):
        ensure_review(state, qid, trigger, events)
    elif decision == "SKIP_NO_EVIDENCE":
        active = next((r for r in state.reviews.values() if r.query_id == qid and r.status == "WAITING_RECALL"), None)
        if active:
            active.status, active.completion_reason = "DONE", "SKIPPED_NO_EVIDENCE"
            active.completed_revision = state.state_revision + 1
            ex = state.executions[qid]
            if active.review_id in ex.blocking_review_ids:
                ex.blocking_review_ids.remove(active.review_id)
                event(events, "REVIEW_BARRIER_REMOVED", query_id=qid, review_id=active.review_id)
            for job in state.jobs.values():
                if job.review_id == active.review_id and job.status not in TERMINAL:
                    job.status, job.lease = "DONE", None
                    event(events, "JOB_COMPLETED", job_id=job.job_id)
            for entry in state.inbox.values():
                if entry.review_id == active.review_id and entry.status == "PENDING":
                    entry.status = "DONE"
            event(events, "REVIEW_COMPLETED", query_id=qid, review_id=active.review_id,
                  mode=active.mode, reason=active.completion_reason)
        state.executions[qid].retry_gate = evidence_signature(state, qid)
        event(events, "MEMORY_SKIPPED", query_id=qid, reason="SKIPPED_NO_EVIDENCE",
              evidence_signature=state.executions[qid].retry_gate, caused_by=trigger)


def next_job(state):
    rank = {qid: i for i, qid in enumerate(topological(state))}
    jobs = [j for j in state.jobs.values() if j.status in {"PENDING", "RUNNING"} and query_ready(state, j.target_query)]
    # Dependency order comes before interface order; RECALL scans complete before
    # the same query's MEMORY begins. Independent branches remain runnable.
    jobs.sort(key=lambda j: (rank[j.target_query], {"CONTEXT_EXPAND": 0, "RECALL": 1, "MEMORY": 2}[j.kind], j.job_id))
    return jobs[0] if jobs else None
