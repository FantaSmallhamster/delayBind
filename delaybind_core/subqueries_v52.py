"""Deterministic V5.2 state transitions, independent of model transports."""

from __future__ import annotations

from .archive import SentenceArchive
from .fact_protocol_v52 import UpdateV52
from .navigation_graph import (
    assert_dual_graph_invariants, descendants, input_signature, parent_bindings,
    query_by_id, query_projection, rebuild_navigation,
)
from .schema_v52 import (
    FactNode, FactUse, QueryExecutionV52, QueryPlanV3, RecallJob, SubqueryStateV52, digest, use_key,
)


def current_uses(state, qid):
    signature = state.executions[qid].input_signature
    return [u for u in state.uses.values() if u.query_id == qid and u.input_signature == signature]


def add_queue(state, qid):
    if qid not in state.review_queue:
        state.review_queue.append(qid)


def store_fact(state, text, refs, window, supersedes=None):
    refs = tuple(sorted(set(refs)))
    fid = "F" + digest([text, refs, supersedes])
    fact = FactNode(fact_id=fid, text=text, source_refs=refs, observed_window=window,
                    supersedes_fact_id=supersedes)
    state.facts.setdefault(fid, fact)
    return fid


def invalidate(state, qids, *, invalidate_root_uses=False, preserve_root_jobs=False):
    affected = descendants(state, qids)
    for qid in affected:
        ex = state.executions[qid]
        if ex.current_binding_id:
            state.binding_store[ex.current_binding_id].valid = False
            ex.current_binding_id = None
        if invalidate_root_uses or qid not in qids:
            for use in current_uses(state, qid):
                use.status = "INVALIDATED"
        ex.status = "DORMANT"
        ex.review_barrier = False
        state.context_requests = {k: v for k, v in state.context_requests.items() if v["query_id"] != qid}
        state.extra_context_refs.pop(qid, None)
        for job in state.recall_jobs.values():
            if preserve_root_jobs and qid in qids:
                continue
            if job.query_id == qid and job.stage != "INVALIDATED":
                job.stage = "INVALIDATED"
    state.review_queue = [q for q in state.review_queue if q not in affected]
    return affected


def schedule_recall(state, qid, reason, trigger=None, *, enabled=True):
    ex = state.executions[qid]
    if ex.status == "DORMANT":
        return
    candidates = sorted(u.fact_id for u in current_uses(state, qid) if u.status == "CANDIDATE")
    if not candidates or not enabled:
        add_queue(state, qid)
        return
    bucket_version = digest(candidates)
    jid = "J" + digest([qid, ex.version, ex.input_signature, bucket_version, trigger, reason])
    if jid not in state.recall_jobs:
        state.recall_jobs[jid] = RecallJob(
            job_id=jid, query_id=qid, input_signature=ex.input_signature, query_version=ex.version,
            bucket_version=bucket_version, reason=reason, trigger_binding_id=trigger,
            candidate_ids=candidates,
        )
    ex.review_barrier = True


def refresh_executions(state, *, reason="BIND_TRIGGER", trigger=None, callback=True):
    # Plan list order need not be topological. Parent validity is authoritative.
    for q in state.plan.queries:
        ex = state.executions[q.id]
        ready = parent_bindings(state, q.id) is not None
        sig = input_signature(state, q.id)
        changed = sig != ex.input_signature or (ready and ex.status == "DORMANT")
        ex.input_signature = sig
        ex.status = "RESOLVED" if ex.current_binding_id else "ACTIVE" if ready else "DORMANT"
        if changed and ready:
            for fid in state.defer_workspace.get(q.id, []):
                key = use_key(q.id, sig, fid)
                if key not in state.uses or state.uses[key].status == "INVALIDATED":
                    state.uses[key] = FactUse(query_id=q.id, input_signature=sig,
                                              fact_id=fid, status="CANDIDATE")
            schedule_recall(state, q.id, reason, trigger, enabled=callback)


class SubqueryRuntime:
    def __init__(self, *, run_id, plan: QueryPlanV3, archive: SentenceArchive, store,
                 callback=True, defer_unbound=True, resume=True):
        self.run_id, self.archive, self.store = run_id, archive, store
        self.callback, self.defer_unbound = callback, defer_unbound
        saved = store.latest_v52_state(run_id) if resume else None
        if saved:
            self.state = SubqueryStateV52.model_validate(saved)
            if self.state.plan.plan_id != plan.plan_id:
                raise ValueError("RESUME_PLAN_MISMATCH")
            archive.watermark = self.state.read_watermark
            assert_dual_graph_invariants(self.state)
        else:
            self.state = SubqueryStateV52(plan=plan, executions={q.id: QueryExecutionV52() for q in plan.queries})
            refresh_executions(self.state)
            self.state.review_queue = []
            rebuild_navigation(self.state)
            self.commit(self.state.model_copy(deep=True), [("PLAN_CREATED", {"plan": plan.model_dump()})])

    def commit(self, staged, events, *, context_id=None, proposal=None):
        staged.state_revision = self.state.state_revision + 1
        rebuild_navigation(staged)
        assert_dual_graph_invariants(staged)
        self.store.append_runtime_events_atomic(
            run_id=self.run_id, events=events, state=staged.model_dump(mode="json"),
            expected_state_revision=self.state.state_revision, context_id=context_id, proposal=proposal)
        self.state = staged  # Publish only after the SQL COMMIT succeeded.

    def ingest_all(self, update: UpdateV52, *, visible_sources, window_index):
        from .source_refs_v52 import SentenceRefResolver
        resolver = SentenceRefResolver(visible_sources)
        staged = self.state.model_copy(deep=True)
        events = []
        for kind, observations in (("facts", update.facts), ("hints", update.hints)):
            for observation in observations:
                resolver.resolve(observation.source_refs)
                self.archive.fetch_sentences(observation.source_refs)
                qids = observation.query_ids if kind == "facts" else []
                if not set(qids) <= set(staged.executions):
                    raise ValueError("UNKNOWN_QUERY_ID")
                if qids and not self.defer_unbound:
                    qids = [q for q in qids if staged.executions[q].status != "DORMANT"]
                    if not qids:
                        continue
                fid = store_fact(staged, observation.text, observation.source_refs, window_index)
                events.append(("FACT_STORED", {"fact_id": fid, "source_refs": list(observation.source_refs)}))
                if kind == "hints":
                    if fid not in staged.hints:
                        staged.hints.append(fid)
                        for q in staged.plan.queries:
                            if staged.executions[q.id].status != "DORMANT":
                                add_queue(staged, q.id)
                    continue
                for qid in sorted(set(qids)):
                    ex = staged.executions[qid]
                    bucket = staged.defer_workspace.setdefault(qid, [])
                    if fid not in bucket:
                        bucket.append(fid)
                    key = use_key(qid, ex.input_signature, fid)
                    if key in staged.uses and staged.uses[key].status != "INVALIDATED":
                        continue
                    dormant = ex.status == "DORMANT"
                    staged.uses[key] = FactUse(query_id=qid, fact_id=fid, input_signature=ex.input_signature,
                                                status="CANDIDATE" if dormant else "PENDING")
                    events.append(("FACT_DEFERRED" if dormant else "FACT_PENDING", {"query_id": qid, "fact_id": fid}))
                    if not dormant:
                        add_queue(staged, qid)
        self.commit(staged, events)

    def record_window(self, window, cursor_state):
        staged = self.state.model_copy(deep=True)
        staged.cursor_state = cursor_state
        staged.pending_window = list(window.source_refs)
        staged.read_watermark = self.archive.watermark
        # A held fragment can now be reviewed against its full sentence. Other
        # context requests become eligible only when new raw actually appears.
        for use in staged.uses.values():
            if use.status != "HELD" or use.input_signature != staged.executions[use.query_id].input_signature:
                continue
            complete = set(window.completed_refs)
            if any(ref.split(":P")[0] in complete for ref in staged.facts[use.fact_id].source_refs):
                refs = staged.extra_context_refs.setdefault(use.query_id, [])
                refs.extend(sorted(complete - set(refs)))
                use.status = "PENDING"
                add_queue(staged, use.query_id)
        self.commit(staged, [("WINDOW_OBSERVED", {"sources": list(window.source_refs), "cursor": cursor_state})])

    def finish_window(self):
        staged = self.state.model_copy(deep=True)
        staged.pending_window = []
        self.commit(staged, [("WINDOW_PROCESSED", {})])

    def close_scope(self):
        if self.state.scope_closed:
            return
        staged = self.state.model_copy(deep=True)
        staged.scope_closed = True
        for q in staged.plan.queries:
            if q.requires_complete_set and staged.executions[q.id].status != "DORMANT":
                add_queue(staged, q.id)
        self.commit(staged, [("SCOPE_CLOSED", {})])

    def apply_memory(self, proposal, context):
        from .memory_transactions import apply_memory_transaction
        return apply_memory_transaction(self, proposal, context)

    def query_projection(self):
        return query_projection(self.state)

    def export(self):
        return self.state.export()
