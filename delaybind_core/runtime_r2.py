"""R2 deterministic state transactions; no natural-language entailment guesses."""

from uuid import uuid4

from .schema_v52 import FactNode, digest
from .schema_r2 import StateR2, Execution, InboxEntry, PendingChunkR2, UpdateRepairProgressR2, fact_id
from .navigation_r2 import (query, query_ready, current_uses, descendants, refresh, assert_invariants,
                            query_projection, binding_effective)
from .review_jobs_r2 import (event, ensure_use, ensure_review, schedule_ready, cancel_reviews,
                             evidence_signature, TERMINAL, enqueue, finish_strict_work)
from .memory_admission_r2 import strict, STRICT_POLICY, grant_admission


class RuntimeR2:
    def __init__(self, *, run_id, plan, archive, store, config):
        self.run_id, self.archive, self.store, self.config = run_id, archive, store, config
        policy = STRICT_POLICY if not config.sentence_splitting else "legacy"
        saved = store.latest_v52_state(run_id)
        if saved:
            self.state = StateR2.model_validate(saved)
            if self.state.plan != plan:
                raise ValueError("RESUME_PLAN_CHANGED")
            if self.state.fact_only != (not config.sentence_splitting):
                raise ValueError("RESUME_FACT_MODE_CHANGED")
            if self.state.admission_policy != policy:
                raise ValueError("RESUME_ADMISSION_POLICY_CHANGED")
            if self.archive is not None:
                self.archive.watermark = self.state.read_watermark
            assert_invariants(self.state)
        else:
            self.state = StateR2(plan=plan, fact_only=not config.sentence_splitting,
                                 admission_policy=policy,
                                 executions={q.id: Execution() for q in plan.queries})
            refresh(self.state)
            self.commit(self.state.model_copy(deep=True), [("PLAN_CREATED", {"plan": plan.model_dump()})])

    def export(self):
        return self.state.export()

    def commit(self, staged, events, *, context_id=None, response=None, receipt=None):
        expected = self.state.state_revision
        for attr in ("facts", "review_records"):
            if any(key not in getattr(staged, attr) or getattr(staged, attr)[key] != old
                   for key, old in getattr(self.state, attr).items()):
                raise ValueError("IMMUTABLE_RECORD_CHANGED:" + attr)
        for bid, old in self.state.binding_store.items():
            new = staged.binding_store.get(bid)
            if new is None or new.model_dump(exclude={"valid"}) != old.model_dump(exclude={"valid"}) or (not old.valid and new.valid):
                raise ValueError("IMMUTABLE_BINDING_CHANGED")
        staged.state_revision = expected + 1
        refresh(staged)
        assert_invariants(staged)
        result = self.store.commit_r2(run_id=self.run_id, state=staged.model_dump(mode="json"), events=events,
                                      expected_revision=expected, context_id=context_id, response=response, receipt=receipt)
        self.state = staged  # Publication happens strictly after durable commit.
        return result

    def record_window(self, window, cursor_state):
        if self.archive is None:
            raise ValueError("ARCHIVE_WINDOW_UNAVAILABLE")
        s = self.state.model_copy(deep=True)
        s.read_watermark, s.cursor_state = self.archive.watermark, cursor_state
        s.pending_window = list(window.source_refs)
        events = [("WINDOW_OBSERVED", {"sources": list(window.source_refs), "cursor": cursor_state})]
        for r in s.reviews.values():
            if r.status != "WAITING_CONTEXT":
                continue
            r.status = "PENDING"
            for j in s.jobs.values():
                if j.review_id == r.review_id and j.status == "WAITING_CONTEXT":
                    j.status = "PENDING"
            enqueue(s, r.query_id, "CONTEXT_EXPAND", "WINDOW_OBSERVED", review_id=r.review_id,
                    payload={"watermark": s.read_watermark, "requests": {k: v.model_dump() for k, v in r.context_requests.items()}}, events=events)
        # An earlier UNBOUND/HOLD can retry only when its requested raw context grows.
        for qid, ex in s.executions.items():
            if ex.current_binding_id or not ex.retry_gate or not query_ready(s, qid):
                continue
            for u in current_uses(s, qid):
                if u.status != "HELD":
                    continue
                previous = next((r for r in reversed(list(s.reviews.values())) if r.query_id == qid and u.use_id in r.context_requests), None)
                if previous:
                    req = previous.context_requests[u.use_id]
                    available = {x.source_ref for x in self.archive.fetch_bounded_context(req.anchor, before=req.before, after=req.after)}
                    if available - set(u.reviewed_source_refs):
                        ex.evidence_revision += 1
                        review = ensure_review(s, qid, "HELD_CONTEXT_AVAILABLE", events)
                        if review:
                            review.extra_refs = sorted(available)
                        break
        self.commit(s, events)

    def record_chunk(self, chunk, cursor_state):
        if self.archive is not None or self.config.update_input_mode != "plain_token_chunks":
            raise ValueError("CHUNK_MODE_REQUIRED")
        if self.state.pending_chunk is not None or chunk.chunk_index != self.state.read_watermark + 1:
            raise ValueError("CHUNK_PROGRESS_MISMATCH")
        s = self.state.model_copy(deep=True)
        s.read_watermark, s.cursor_state = chunk.chunk_index, cursor_state
        s.pending_chunk = PendingChunkR2(**vars(chunk))
        self.commit(s, [("CHUNK_OBSERVED", {"chunk_index": chunk.chunk_index,
                                             "token_start": chunk.token_start, "token_end": chunk.token_end,
                                             "chunk_hash": digest(chunk.chunk_text), "cursor": cursor_state})])

    def finish_chunk(self):
        if self.state.pending_chunk is None:
            raise ValueError("NO_PENDING_CHUNK")
        s = self.state.model_copy(deep=True)
        index = s.pending_chunk.chunk_index
        s.pending_chunk = None
        s.update_repair_progress = None
        self.commit(s, [("CHUNK_PROCESSED", {"chunk_index": index})])

    def finish_window(self):
        s = self.state.model_copy(deep=True)
        s.pending_window = []
        s.update_repair_progress = None
        self.commit(s, [("WINDOW_PROCESSED", {})])

    def ingest(self, update, context, *, rejected=(), repair_progress=None):
        import json
        row = self.store.connection.execute("SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
                                            (self.run_id, context["context_id"])).fetchone()
        if not row or json.loads(row[0]) != {"interface": "UPDATE_SNAPSHOT", **context}:
            raise ValueError("UPDATE_CONTEXT_NOT_REGISTERED")
        if "chunk_text" in context:
            pending = self.state.pending_chunk
            if (self.archive is not None or not context.get("fact_only") or not context.get("member_bindings")
                    or pending is None or pending.chunk_text != context["chunk_text"]
                    or pending.chunk_index != context.get("chunk_index")
                    or context["window_sources"] or context["working_memory"]["raw_evidence"]):
                raise ValueError("CHUNK_UPDATE_CONTEXT_MISMATCH")
        else:
            for raw in context["window_sources"] + context["working_memory"]["raw_evidence"]:
                if self.archive.fetch_sentence(raw["source_ref"]).text_sha256 != raw["text_sha256"]:
                    raise ValueError("RAW_INTEGRITY_ERROR")
        if update.context_id != context["context_id"]:
            raise ValueError("UPDATE_CONTEXT_MISMATCH")
        allowed_revision = self.state.update_receipts.get(update.context_id, context["state_revision"])
        if self.state.state_revision != allowed_revision:
            raise ValueError("STALE_UPDATE_CONTEXT")
        from .source_refs_v52 import SentenceRefResolver
        fact_only = bool(context.get("fact_only"))
        resolver = None if fact_only else SentenceRefResolver(context["visible_sources"])
        snapshot = {q["id"]: q for q in context["query_graph"]["queries"]}
        s, events, changed = self.state.model_copy(deep=True), [], set()
        if rejected:
            event(events, "UPDATE_ITEMS_REJECTED", context_id=update.context_id, rejected_items=list(rejected))
        for observation in sorted(update.facts, key=lambda x: (x.text, x.source_refs)):
            if fact_only:
                if observation.source_refs:
                    raise ValueError("FACT_ONLY_UPDATE_HAS_SOURCE_REFS")
            else:
                resolver.resolve(observation.source_refs)
            fid = fact_id(observation.text, observation.source_refs)
            if fid not in s.facts:
                s.facts[fid] = FactNode(fact_id=fid, text=observation.text, source_refs=tuple(sorted(observation.source_refs)),
                                       observed_window=s.read_watermark)
                event(events, "FACT_STORED", fact_id=fid, source_refs=observation.source_refs)
            for route in sorted(observation.routes, key=lambda r: r.query_id):
                qid = route.query_id
                if qid not in snapshot or route.observed_status != snapshot[qid]["status"]:
                    raise ValueError("QUERY_STATUS_ECHO_MISMATCH")
                ex = s.executions[qid]
                if ex.version != snapshot[qid]["version"] or ex.input_signature != snapshot[qid]["input_signature"]:
                    raise ValueError("STALE_UPDATE_QUERY")
                if not self.config.defer_unbound and route.observed_status == "DORMANT":
                    continue
                bucket = s.route_index.setdefault(qid, [])
                newly_routed = fid not in bucket
                if fid in bucket and not strict(s):
                    continue
                if newly_routed:
                    bucket.append(fid)
                    bucket.sort()
                    changed.add(qid)
                    ex.evidence_revision += 1
                    event(events, "FACT_ROUTED", query_id=qid, fact_id=fid, observed_status=route.observed_status)
                status = "CANDIDATE" if route.observed_status == "DORMANT" else "PENDING"
                u = ensure_use(s, qid, fid, status=status)
                if status == "PENDING":
                    lane = "REBIND" if ex.current_binding_id else "BIND"
                    iid = "IN" + digest([u.use_id, update.context_id])
                    if iid in s.inbox:
                        continue
                    if strict(s):
                        if not grant_admission(s, u, "UPDATE", update.context_id):
                            continue
                        if u.status not in {"ACCEPTED", "PENDING"}:
                            u.status = "PENDING"
                        if not newly_routed:
                            ex.evidence_revision += 1
                        changed.add(qid)
                    s.inbox[iid] = InboxEntry(entry_id=iid, fact_id=fid, use_key=u.use_id, query_id=qid, lane=lane,
                                              update_context_id=update.context_id, observed_status=route.observed_status,
                                              source_refs=list(observation.source_refs), evidence_revision=ex.evidence_revision)
                    event(events, "INBOX_ENQUEUED", entry_id=iid, query_id=qid, lane=lane)
        for hint in update.hints:
            if fact_only:
                if hint.source_refs:
                    raise ValueError("FACT_ONLY_HINT_HAS_SOURCE_REFS")
            else:
                resolver.resolve(hint.source_refs)
            fid = fact_id(hint.text, hint.source_refs)
            if fid not in s.facts:
                s.facts[fid] = FactNode(fact_id=fid, text=hint.text, source_refs=tuple(sorted(hint.source_refs)),
                                       observed_window=s.read_watermark)
            if fid not in s.hints:
                s.hints.append(fid)
        # All routes exist before ancestor barriers are installed and jobs wake.
        from .navigation_r2 import topological
        for qid in topological(s):
            if qid in changed and snapshot[qid]["status"] != "DORMANT":
                if strict(s):
                    schedule_ready(s, qid, "UPDATE", events, callback=self.config.enable_defer_callback)
                else:
                    ensure_review(s, qid, "UPDATE", events)
        s.update_receipts[update.context_id] = self.state.state_revision + 1
        if repair_progress is not None:
            s.update_repair_progress = UpdateRepairProgressR2.model_validate(repair_progress)
        self.commit(s, events)
        return {"changed_query_ids": sorted(changed), "enqueued_job_ids": [p["job_id"] for e, p in events if e == "JOB_ENQUEUED"]}

    def claim(self, job_id):
        s = self.state.model_copy(deep=True)
        j = s.jobs[job_id]
        if j.status not in {"PENDING", "RUNNING"}:
            raise ValueError("JOB_NOT_CLAIMABLE")
        if j.status == "RUNNING":
            j.retry_count += 1
        j.status, j.lease = "RUNNING", str(uuid4())
        if j.review_id:
            s.reviews[j.review_id].sent = True
        self.commit(s, [("JOB_CLAIMED", {"job_id": job_id, "lease": j.lease})])
        return self.state.jobs[job_id]

    def finish_recall_batch(self, job_id, selected, *, batch_size=None, expected_offset=None):
        s, events = self.state.model_copy(deep=True), []
        j = s.jobs[job_id]
        p = j.payload
        offset = p["offset"] if expected_offset is None else expected_offset
        response_hash = digest(sorted(set(selected)))
        receipts = p.setdefault("batch_receipts", {})
        if str(offset) in receipts:
            if receipts[str(offset)] != response_hash:
                raise ValueError("CONTEXT_CONSUMED")
            return
        if j.status not in {"PENDING", "RUNNING"} or offset != p["offset"]:
            raise ValueError("STALE_RECALL_CONTEXT")
        ex = s.executions[j.target_query]
        if j.input_signature != ex.input_signature or j.query_version != ex.version or not query_ready(s, j.target_query):
            raise ValueError("STALE_RECALL_CONTEXT")
        batch = p["candidates"][p["offset"]:p["offset"] + (batch_size or self.config.candidate_batch_size)]
        if not set(selected) <= set(batch):
            raise ValueError("RECALL_ID_NOT_SELECTABLE")
        p["selected"] = sorted(set(p["selected"]) | set(selected))
        p["offset"] += len(batch)
        receipts[str(offset)] = response_hash
        j.status, j.lease = "PENDING", None
        if p["offset"] == len(p["candidates"]):
            j.status = "DONE"
            for fid in p["selected"]:
                use = ensure_use(s, j.target_query, fid, status="CANDIDATE", origin="RECALL")
                if strict(s):
                    grant_admission(s, use, "RECALL", job_id)
                    if use.status != "ACCEPTED":
                        use.status = "PENDING"
                else:
                    use.status = "PENDING"
            ex.evidence_revision += 1
            if strict(s):
                finish_strict_work(s, j.target_query, "RECALL", events)
            else:
                ensure_review(s, j.target_query, "RECALL", events)
            event(events, "JOB_COMPLETED", job_id=job_id)
        event(events, "RECALL_BATCH_SCANNED", job_id=job_id, selected=selected, batch=batch)
        self.commit(s, events)

    def close_scope(self):
        if self.state.scope_closed:
            return
        s, events = self.state.model_copy(deep=True), [("SCOPE_CLOSED", {})]
        s.scope_closed = True
        resumed = set()
        for r in s.reviews.values():
            if r.status in {"WAITING_CONTEXT", "WAITING_SCOPE"}:
                waiting_for_scope = r.status == "WAITING_SCOPE"
                r.status = "PENDING"
                r.evidence_signature = evidence_signature(s, r.query_id)
                resumed.add(r.query_id)
                for j in s.jobs.values():
                    if j.review_id == r.review_id and j.status in {"WAITING_CONTEXT", "WAITING_SCOPE"}:
                        j.status = "PENDING"
                if waiting_for_scope and any(j.review_id == r.review_id and j.kind == "MEMORY" and j.status == "PENDING"
                                             for j in s.jobs.values()):
                    event(events, "COLLECTION_FINALIZATION_RESUMED", query_id=r.query_id,
                          review_id=r.review_id, caused_by="SCOPE_CLOSED")
        for q in s.plan.queries:
            if (q.id not in resumed and query_ready(s, q.id)
                    and (q.requires_complete_set or any(u.status == "HELD" for u in current_uses(s, q.id)))):
                if strict(s):
                    schedule_ready(s, q.id, "SCOPE_CLOSED", events,
                                   callback=self.config.enable_defer_callback, force_review=True)
                else:
                    ensure_review(s, q.id, "SCOPE_CLOSED", events)
        self.commit(s, events)

    def fail(self, reason, *, job_id=None, resource=False):
        s = self.state.model_copy(deep=True)
        s.status = "RESOURCE_LIMIT" if resource else "RUNTIME_ERROR"
        if reason not in s.reason_codes:
            s.reason_codes.append(reason)
        if job_id and s.jobs[job_id].kind == "RECALL":
            s.jobs[job_id].status = "SCAN_INCOMPLETE"
        self.commit(s, [("RUN_STATUS", {"status": s.status, "reason": reason, "job_id": job_id})])

    def expand_context(self, job_id):
        if self.archive is None:
            raise ValueError("CONTEXT_EXPANSION_UNAVAILABLE")
        s, events = self.state.model_copy(deep=True), []
        job = s.jobs[job_id]
        r = s.reviews[job.review_id]
        changed = False
        for key, req in r.context_requests.items():
            raw = self.archive.fetch_bounded_context(req.anchor, before=req.before, after=req.after)
            old = set(r.extra_refs)
            record_id = r.staged_review_ids.get(key)
            if record_id:
                old |= set(s.review_records[record_id].raw_hashes)
            new = {x.source_ref for x in raw} - old
            if not new:
                continue
            if s.context_expansions >= self.config.max_context_expansions:
                raise ValueError("CONTEXT_EXPANSION_BUDGET")
            s.context_expansions += 1
            r.budget["context_expansions"] = r.budget.get("context_expansions", 0) + 1
            r.extra_refs = sorted(set(r.extra_refs) | new)
            r.staged_review_ids.pop(key, None)
            changed = True
            event(events, "RAW_CONTEXT_EXPANDED", review_id=r.review_id, source_refs=sorted(new))
        job.status, job.lease = "DONE", None
        if changed:
            s.executions[r.query_id].evidence_revision += 1
            r.inbox_revision = s.executions[r.query_id].evidence_revision
            r.evidence_signature = evidence_signature(s, r.query_id)
        # No future reads are permitted here. Keep the barrier but let the cursor
        # and independent branches advance. At EOF FINAL must decide UNBOUND.
        waiting = not changed and not s.scope_closed and not (set(r.required_use_ids) - set(r.staged_review_ids))
        r.status = "WAITING_CONTEXT" if waiting else "PENDING"
        for j in s.jobs.values():
            if j.review_id == r.review_id and j.kind == "MEMORY" and j.status not in TERMINAL:
                j.status, j.lease = r.status, None
        event(events, "CONTEXT_EXPANSION_COMPLETED", review_id=r.review_id, waiting=waiting, changed=changed)
        self.commit(s, events)


def invalidate_closure(state, roots, events, *, preserve_root_uses=False, except_review=None):
    affected = set(roots) | descendants(state, roots)
    for qid in sorted(affected):
        ex = state.executions[qid]
        bid = ex.current_binding_id
        if bid:
            state.binding_store[bid].valid = False
            ex.current_binding_id = None
            event(events, "BINDING_RETIRED", binding_id=bid, query_id=qid)
        if not (preserve_root_uses and qid in roots):
            for u in current_uses(state, qid):
                u.status = "INVALIDATED"
                event(events, "FACT_USE_INVALIDATED", use_id=u.use_id, query_id=qid)
            ex.retry_gate = None
        for r in state.reviews.values():
            if r.query_id == qid and r.review_id != except_review and r.status not in TERMINAL:
                r.status = "CANCELLED"
                if r.review_id in ex.blocking_review_ids:
                    ex.blocking_review_ids.remove(r.review_id)
        for j in state.jobs.values():
            if j.target_query == qid and j.review_id != except_review and j.status not in TERMINAL:
                j.status, j.lease = "CANCELLED", None
                event(events, "JOB_CANCELLED", job_id=j.job_id, query_id=qid)
        for entry in state.inbox.values():
            if entry.query_id == qid and not (preserve_root_uses and qid in roots):
                entry.status = "INVALIDATED"
    return affected
