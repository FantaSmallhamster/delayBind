"""Text-wire R2 streaming loop and durable per-query maintenance dispatcher."""

from collections import Counter
from dataclasses import asdict, replace
import sqlite3
from uuid import uuid4

from .agents_r2 import HighLevelAgentR2, LowLevelAgentR2
from .api import ModelAPIError
from .archive import SentenceArchive
from .context_r2 import (
    budgeted_evidence_pack,
    build_memory_context,
    build_update_context,
    build_chunk_update_context,
    evidence_pack,
    persist_memory_context,
)
from .cursor_v52 import SentenceReadCursor
from .member_graph_r2 import MemberBranchLimit, projected_value
from .member_memory_view_r2 import MEMBER_MEMORY_VIEW_VERSION
from .memory_r2 import apply_memory
from .memory_admission_r2 import strict
from .navigation_r2 import query_projection, render_query
from .plan_goal_r2 import parse_v51_member_plan
from .plan_repair_r2 import (build_repair_context, register_repair_context, repair_basis_key,
                             record_plan_repair_rejection, PlanRepairProposalError, apply_repair,
                             REJECTION_POLICY_VERSION)
from .cursor_plain_r2 import PlainTokenChunkCursor, PLAIN_CHUNK_VERSION
from .prompts_r2 import (
    MEMBER_MEMORY_PROMPT_VERSION,
    MEMBER_PROMPT_VERSION,
    MEMBER_RECALL_PROMPT_VERSION,
    MEMORY_BIND_PROMPT_VERSION,
    PROMPT_VERSION,
    messages,
    prompt_version_for,
)
from .protocol_r2 import RepairScope, parse_memory, parse_update
from .review_jobs_r2 import next_job
from .runtime_r2 import RuntimeR2
from .schema_r2 import (
    CONTRACT,
    PROTOCOL,
    EvidencePlanR2,
    StateR2,
    UpdateResponseR2,
    LEGACY_MEMORY_INTERFACE,
    UNIFIED_MEMORY_INTERFACE,
    UNIFIED_MEMORY_VERSION,
    member_plan,
)
from .schema_v52 import QueryPlanV3, digest
from .answer_wire_r2 import extract_boxed, V52ResourceLimit, V52ProtocolError
from .text_protocol_v52 import parse_selection
from .text_views_v52 import memory_view, plain_text_view, raw_view
from .token_budget import TokenCounter


def _query_projection_row(state, query_id, *, instantiate_members=False):
    graph = query_projection(state, instantiate_members=instantiate_members)
    return next(row for row in graph["queries"] if row["id"] == query_id)


async def run_subqueries_r2(runner, *, run_id, sample, manifest, store, plan=None):
    cfg = runner.config
    unified_memory = cfg.memory_interface == UNIFIED_MEMORY_INTERFACE
    plain_chunks = cfg.update_input_mode == "plain_token_chunks"
    if plain_chunks and (sample.context is None or runner.tokenizer is None):
        raise ValueError("PLAIN_CHUNKS_REQUIRE_CONTEXT_AND_TOKENIZER")
    if cfg.answer_format == "auto":
        cfg = replace(cfg, answer_format="boxed")
    if manifest is None and (cfg.sentence_splitting or sample.context is None):
        from .data import build_manifest
        from .sentence_index import text_manifest
        manifest = text_manifest(sample.context, sample_id=sample.sample_id) if sample.context is not None else build_manifest(sample)
    if plan is not None and not isinstance(plan, (QueryPlanV3, EvidencePlanR2)):
        raise ValueError("PLAN_PROTOCOL_MISMATCH:R2 requires QueryPlanV3 internally")
    counter = TokenCounter(runner.tokenizer, encoding_name=cfg.tokenizer_encoding)
    reader = runner.reader_client or runner.client
    high, low = HighLevelAgentR2(runner.client), LowLevelAgentR2(reader)
    archive = None if plain_chunks else SentenceArchive(store, run_id)
    saved = store.latest_v52_state(run_id)
    if saved and plan is None:
        plan = StateR2.model_validate(saved).plan
    elif plan is not None and not saved:
        plan = member_plan(plan)
    logical_calls = sum(m.get("kind") == "MODEL_REQUEST" for m in store.list_context_manifests(run_id))
    repair_skips = 0

    def request(interface, payload):
        msg = messages(interface, payload)
        return msg, counter.serialized({"messages": msg})

    def memory_fits(wm):
        rendered = memory_view(wm, include_raw=cfg.sentence_splitting,
                               include_sources=cfg.sentence_splitting)
        return (counter.count(rendered) <= cfg.memory_token_budget if cfg.memory_token_budget is not None
                else len(rendered) <= cfg.memory_char_budget)

    async def complete(interface, payload):
        nonlocal logical_calls
        reserve = 0 if interface == "ANSWER" else cfg.answer_reserve_calls
        if logical_calls >= cfg.max_model_calls - reserve:
            raise V52ResourceLimit("MODEL_CALL_BUDGET")
        original = payload.get("original_memory_request", payload)
        unified_review = unified_memory and interface in {"MEMORY", "MEMORY_REPAIR"}
        msg, tokens = request(interface, payload)
        wm = original.get("working_memory")
        final = interface == "ANSWER" or original.get("phase") == "FINAL"
        budget_error = ("FINAL_RAW_MEMORY_BUDGET" if cfg.sentence_splitting else "FINAL_FACT_MEMORY_BUDGET") if final else "REVIEW_INPUT_BUDGET" if interface.startswith("MEMORY") else interface + "_INPUT_BUDGET"
        if unified_review:
            budget_error = "UNIFIED_MEMORY_INPUT_BUDGET"
        # Candidate evidence is temporary inspection material. Only actual
        # request/context limits apply; the resident-memory budget is for ANSWER.
        if wm is not None and not unified_review and not memory_fits(wm):
            raise V52ResourceLimit(budget_error)
        limit = cfg.max_answer_input_tokens if interface == "ANSWER" else cfg.max_review_input_tokens if interface.startswith("MEMORY") else cfg.max_input_tokens
        if interface in high.interfaces:
            agent = high
        elif interface in low.interfaces:
            agent = low
        else:
            raise PermissionError(f"R2_INTERFACE_UNKNOWN:{interface}")
        output = getattr(getattr(agent.client, "config", None), "max_tokens", cfg.answer_reserve_tokens)
        if interface == "ANSWER":
            output = max(output, cfg.answer_reserve_tokens)
        if tokens > limit or tokens + output > cfg.max_context_tokens:
            raise V52ResourceLimit(budget_error)
        raws = (wm or {}).get("raw_evidence", []) + original.get("window_sources", [])
        raw_body = original.get("chunk_text")
        audit = dict(protocol_version=PROTOCOL, memory_mode=original.get("allowed_mode"),
                     review_phase=original.get("phase"), context_id=original.get("context_id"),
                     review_id=original.get("review_id"), raw_bundle_hash=digest(raw_body if raw_body is not None else raws),
                     agent_role=agent.role,
                     client_model=getattr(getattr(agent.client, "config", None), "model", type(agent.client).__name__))
        if interface in {"MEMORY", "MEMORY_REPAIR"} and original.get("review_id"):
            session = runtime.state.reviews[original["review_id"]]
            audit.update(authorized_fact_count=len(original.get("allowed_fact_ids", [])),
                         update_admission_count=sum(runtime.state.uses[key].admission_origin == "UPDATE"
                                                    for key in session.admitted_use_tokens),
                         recall_admission_count=sum(runtime.state.uses[key].admission_origin == "RECALL"
                                                    for key in session.admitted_use_tokens),
                         prior_support_count=len(session.prior_support_use_ids),
                         admission_digest=original.get("admission_digest", ""))
            if unified_review:
                audit.update(memory_interface_version=UNIFIED_MEMORY_VERSION,
                             evidence_origins=original.get("evidence_origins", []),
                             query_id=original["query_id"],
                             branch_id=(original.get("branch") or {}).get("branch_id"),
                             input_signature=original.get("input_signature"),
                             evidence_digest=original.get("evidence_digest"),
                             authorized_use_ids=original.get("authorized_use_ids", []),
                             evidence_fact_count=len(original.get("allowed_fact_ids", [])),
                             rendered_fact_count=len(original.get("fact_aliases", {})))
        logical_calls += 1
        call_id = "R2CALL-" + str(uuid4())
        store.save_context_manifest(run_id, call_id, {"kind": "MODEL_REQUEST", "interface": interface,
            "call_id": call_id,
            **audit, "plan_mode": original.get("mode", "INITIAL") if interface == "PLAN" else None,
            "prompt_version": prompt_version_for(interface, payload), "prompt_hash": digest(msg), "input_tokens": tokens,
            "memory_view_version": (UNIFIED_MEMORY_VERSION if unified_review else MEMBER_MEMORY_VIEW_VERSION if not cfg.sentence_splitting
                                    and "members" in (wm or {}).get("navigation", {}) else None),
            "output_token_budget": output, "tokenizer": counter.identity,
            "raw_input_tokens": (counter.count(raw_body) if raw_body is not None else
                                 counter.count(plain_text_view(raws) if not cfg.sentence_splitting else raw_view(raws)) if raws else 0),
            **({"raw_input_kind": "plain_token_chunk"} if raw_body is not None else {}),
            "visible_source_refs": [] if not cfg.sentence_splitting else [r["source_ref"] for r in raws]})
        local_metadata = {key: audit[key] for key in (
            "protocol_version", "memory_mode", "review_phase", "context_id", "review_id", "raw_bundle_hash")}
        if unified_review:
            local_metadata["memory_interface_version"] = UNIFIED_MEMORY_VERSION
        raw = await agent.call(interface, run_id=run_id, messages=msg, response_schema=None,
                               extra={"max_tokens": output}, local_metadata=local_metadata)
        if unified_review:
            # Real API clients require an explicit normal finish. Scripted
            # clients may return plain strings, which represent complete fixtures.
            if hasattr(raw, "finish_reason") and raw.finish_reason != "stop":
                raise ModelAPIError(f"INCOMPLETE_MODEL_RESPONSE:{raw.finish_reason or 'missing_finish_reason'}")
            store.save_context_manifest(run_id, call_id + "-response", {
                "kind": "MODEL_RESPONSE", "call_id": call_id, "interface": interface,
                "memory_interface_version": UNIFIED_MEMORY_VERSION,
                "finish_reason": getattr(raw, "finish_reason", "scripted_complete"),
                "memory_input_tokens": getattr(raw, "input_tokens", None) or tokens,
                "memory_output_tokens": (getattr(raw, "output_tokens", None)
                                         if getattr(raw, "output_tokens", None) is not None else counter.count(raw)),
                "token_source": "provider" if getattr(raw, "output_tokens", None) is not None else "local_tokenizer"})
        return raw

    if plan is None:
        error = None
        validation_errors = []
        for _ in range(cfg.max_plan_retries + 1):
            try:
                raw = await complete("PLAN", {"question": sample.question, "validation_errors": validation_errors,
                                              "fact_only": not cfg.sentence_splitting, "member_bindings": True})
                plan = parse_v51_member_plan(raw, sample.question)
                break
            except (ValueError, TimeoutError) as exc:
                error = str(exc)
                if isinstance(exc, ValueError):
                    validation_errors.append(error)
        if plan is None:
            raise V52ProtocolError("PLAN_PROTOCOL_ERROR:" + str(error))
    runtime = RuntimeR2(run_id=run_id, plan=plan, archive=archive, store=store, config=cfg)
    config_metadata = asdict(cfg)
    if cfg.update_input_mode == "archive_windows":
        config_metadata.pop("update_input_mode")
    if cfg.plan_repair_failure_policy == "abort":
        config_metadata.pop("plan_repair_failure_policy")
    if cfg.memory_interface == LEGACY_MEMORY_INTERFACE:
        config_metadata.pop("memory_interface")
    metadata = dict(protocol_version=PROTOCOL, memory_contract=CONTRACT, prompt_version=PROMPT_VERSION,
                    execution_policy_version=("r2-low-answer-strict-recall-v1" if runtime.state.fact_only
                                              else "r2-low-answer-raw-v1"),
                    admission_policy=runtime.state.admission_policy,
                    answer_agent_role="LOW",
                    answer_model=getattr(getattr(reader, "config", None), "model", type(reader).__name__),
                    empty_memory_policy=("evidence-or-explicit-upstream-v1" if runtime.state.fact_only
                                         else "legacy"),
                    question_hash=digest(sample.question),
                    manifest_hash=digest(manifest.model_dump(mode="json") if manifest is not None else sample.context),
                    config=config_metadata, tokenizer=counter.identity,
                    high_model=getattr(getattr(runner.client, "config", None), "model", type(runner.client).__name__),
                    low_model=getattr(getattr(reader, "config", None), "model", type(reader).__name__))
    if plain_chunks:
        metadata.update(update_input_version=PLAIN_CHUNK_VERSION, chunk_size=cfg.chunk_size,
                        chunk_preprocess="context.strip() once", chunk_truncation="none",
                        tokenizer_identity=getattr(runner.tokenizer, "name_or_path", type(runner.tokenizer).__name__))
    if cfg.plan_repair_failure_policy == "continue_valid_plan":
        metadata["plan_repair_control_version"] = REJECTION_POLICY_VERSION
    if not cfg.sentence_splitting:
        metadata["memory_bind_prompt_version"] = MEMBER_MEMORY_PROMPT_VERSION if isinstance(plan, EvidencePlanR2) else MEMORY_BIND_PROMPT_VERSION
    if isinstance(plan, EvidencePlanR2):
        metadata["prompt_version"] = MEMBER_PROMPT_VERSION
        metadata["update_prompt_version"] = prompt_version_for("UPDATE", {
            "member_bindings": True,
            "fact_only": runtime.state.fact_only,
            "plan_hints_enabled": runtime.state.fact_only and cfg.plan_repair_mode == "on_hint",
            **({"chunk_text": ""} if plain_chunks else {}),
        })
        metadata["recall_prompt_version"] = MEMBER_RECALL_PROMPT_VERSION
        metadata["binding_graph_contract"] = "r2-members-1"
        if not cfg.sentence_splitting:
            metadata["memory_view_version"] = MEMBER_MEMORY_VIEW_VERSION
    if unified_memory:
        metadata.update(memory_interface=UNIFIED_MEMORY_INTERFACE,
                        memory_interface_version=UNIFIED_MEMORY_VERSION,
                        memory_bind_prompt_version=UNIFIED_MEMORY_VERSION,
                        memory_view_version=UNIFIED_MEMORY_VERSION,
                        execution_policy_version="r2-low-answer-unified-evidence-v1",
                        recall_prompt_version=None)
    if runtime.state.run_metadata and runtime.state.run_metadata != metadata:
        raise ValueError("RESUME_CONFIGURATION_CHANGED")
    if not runtime.state.run_metadata:
        staged = runtime.state.model_copy(deep=True)
        staged.run_metadata = metadata
        runtime.commit(staged, [("RUN_CONFIGURED", metadata)])
    if plain_chunks:
        cursor = PlainTokenChunkCursor(sample.context, runner.tokenizer, chunk_size=cfg.chunk_size,
                                       state=runtime.state.cursor_state or None)
        if runtime.state.cursor_state and runtime.state.read_watermark != cursor.window_index - 1:
            raise ValueError("CURSOR_WATERMARK_MISMATCH")
        if not runtime.state.cursor_state and runtime.state.read_watermark != -1:
            raise ValueError("CURSOR_STATE_MISSING")
        pending = runtime.state.pending_chunk
        if pending is not None:
            start = pending.chunk_index * cfg.chunk_size
            end = min(start + cfg.chunk_size, len(cursor.tokens))
            if (pending.chunk_index != cursor.window_index - 1 or pending.token_start != start
                    or pending.token_end != end or cursor.position != end
                    or pending.chunk_text != runner.tokenizer.decode(cursor.tokens[start:end])):
                raise ValueError("PENDING_CHUNK_CURSOR_MISMATCH")
        elif runtime.state.update_repair_progress is not None:
            raise ValueError("UPDATE_PROGRESS_WITHOUT_PENDING_CHUNK")
    elif cfg.sentence_splitting:
        cursor = SentenceReadCursor(manifest, archive, counter=counter, window_mode=cfg.window_mode,
                                    state=runtime.state.cursor_state or None)
    else:
        from .cursor_legacy_v52 import LegacyReadCursor
        cursor = LegacyReadCursor(archive, text=sample.context, sample_id=sample.sample_id, manifest=manifest,
                                  tokenizer=runner.tokenizer, state=runtime.state.cursor_state or None)

    async def update(refs=None, *, chunk=None):
        progress = runtime.state.update_repair_progress if chunk is not None else None
        if progress:
            matches = [m for m in store.list_context_manifests(run_id)
                       if m.get("interface") == "UPDATE_SNAPSHOT" and m.get("context_id") == progress.context_id]
            if (len(matches) != 1 or matches[0].get("chunk_text") != chunk.chunk_text
                    or matches[0].get("chunk_index") != chunk.chunk_index
                    or progress.context_id not in runtime.state.update_receipts):
                raise ValueError("UPDATE_RESUME_SNAPSHOT_MISMATCH")
            payload = {k: v for k, v in matches[0].items() if k != "interface"}
            if progress.complete:
                runtime.finish_chunk()
                return
            scope = RepairScope(progress.original_rejected, UpdateResponseR2(context_id=payload["context_id"]))
            scope.targets = list(progress.repair_targets)
            scope.retained = {(x[0], x[1], tuple(x[2])) for x in progress.retained_identities}
            original_rejected, retained, error = (list(progress.original_rejected),
                                                  list(progress.retained_items), list(progress.validation_errors))
            first_attempt = progress.next_attempt
        else:
            if chunk is None:
                payload = build_update_context(runtime, sample.question, refs, counter=counter)
            else:
                matches = [m for m in store.list_context_manifests(run_id)
                           if m.get("interface") == "UPDATE_SNAPSHOT" and m.get("chunk_text") == chunk.chunk_text
                           and m.get("chunk_index") == chunk.chunk_index
                           and m.get("state_revision") == runtime.state.state_revision]
                if any(m.get("context_id") in runtime.state.update_receipts for m in matches):
                    raise ValueError("UPDATE_RESUME_PROGRESS_MISSING")
                payload = ({k: v for k, v in matches[-1].items() if k != "interface"} if matches else
                           build_chunk_update_context(runtime, sample.question, chunk, counter=counter))
            scope, original_rejected, retained, error, first_attempt = None, [], [], [], 0
        for attempt in range(first_attempt, cfg.max_protocol_retries + 1):
            data = payload if attempt == 0 else {**payload, "rejected_items": original_rejected,
                "retained_items": retained, "repair_targets": scope.targets, "validation_errors": error}
            raw = await complete("UPDATE" if attempt == 0 else "UPDATE_REPAIR", data)
            try:
                observations, rejected = parse_update(raw, payload)
            except ValueError as exc:
                observations, rejected = UpdateResponseR2(context_id=payload["context_id"]), [{"item": raw, "error": str(exc)}]
            if scope is not None:
                observations, scope_errors = scope.restrict(observations)
                rejected.extend(scope_errors)
            elif rejected:
                original_rejected = rejected
                scope = RepairScope(rejected, observations)
            new_retained = retained + [f.model_dump() for f in [*observations.facts, *observations.hints]]
            repair_progress = None
            if chunk is not None:
                repair_progress = dict(context_id=payload["context_id"], next_attempt=attempt + 1,
                                       complete=not rejected, original_rejected=original_rejected,
                                       retained_items=new_retained,
                                       repair_targets=scope.targets if scope else [],
                                       retained_identities=[[qid, body, list(refs)] for qid, body, refs in sorted(scope.retained)] if scope else [],
                                       validation_errors=list(rejected))
            runtime.ingest(observations, payload, rejected=rejected, repair_progress=repair_progress)
            retained = new_retained
            if not rejected:
                runtime.finish_chunk() if chunk is not None else runtime.finish_window()
                return
            error = rejected
        raise V52ProtocolError("UPDATE_REPAIR_EXHAUSTED")

    async def repair_plan():
        nonlocal repair_skips
        if cfg.plan_repair_mode != "on_hint" or not set(runtime.state.hints) - set(runtime.state.processed_hints):
            return
        recoverable = cfg.plan_repair_failure_policy == "continue_valid_plan"
        ctx = build_repair_context(runtime, sample.question, persist=not recoverable)
        key = repair_basis_key(runtime.state, ctx) if recoverable else None
        if recoverable:
            if key in runtime.state.plan_repair_failures:
                repair_skips += 1
                return
            register_repair_context(runtime, ctx)
        data = dict(ctx)
        errors, response_hashes = [], []
        for _ in range(cfg.max_protocol_retries + 1):
            raw = await complete("PLAN", data)
            response_hashes.append(digest(raw))
            try:
                apply_repair(runtime, raw, ctx)
                return
            except PlanRepairProposalError as exc:
                errors.append(exc.code)
                data["validation_errors"] = str(exc)
        if not recoverable:
            raise V52ProtocolError("PLAN_REPAIR_EXHAUSTED")
        record_plan_repair_rejection(runtime, ctx, key, errors, response_hashes)

    def memory_payload(ctx):
        s = runtime.state
        session = s.reviews[ctx.review_id]
        old_binding = s.binding_store[ctx.expected_binding_id].model_dump() if ctx.expected_binding_id else None
        if old_binding is not None and ctx.fact_only:
            old_binding.pop("source_refs", None)
            old_binding.pop("decision_source_refs", None)
        instance = _query_projection_row(s, ctx.query_id)
        display_branch = ctx.branch or (
            session.branches[0]
            if ctx.member_bindings and len(session.branches) == 1
            else None
        )
        if display_branch:
            instance["bound_inputs"] = display_branch.bound_inputs
            instance["rendered_query"] = render_query(
                instance["template"], display_branch.bound_inputs
            )
            if old_binding and ctx.branch:
                selected = [
                    member
                    for member in s.binding_store[ctx.expected_binding_id].members
                    if member.branch_id == ctx.branch.branch_id
                ]
                old_binding = (
                    {
                        **old_binding,
                        "value": projected_value(selected),
                        "members": [member.model_dump() for member in selected],
                    }
                    if selected
                    else None
                )
        if unified_memory:
            return {**ctx.model_dump(mode="json"), "query_instance": instance}
        return {"question": sample.question, **ctx.model_dump(mode="json"),
                "query_instance": instance,
                "old_binding": old_binding,
                "staged_reviews": [{**s.review_records[key].review.model_dump(),
                    "corrected_fact_id": s.review_records[key].corrected_fact_id,
                    "record_hash": s.review_records[key].record_hash} for key in session.staged_review_ids.values()]}

    async def review(job):
        size = cfg.candidate_batch_size
        while True:
            ctx = build_memory_context(runtime, job.review_id, batch_size=size)
            if strict(runtime.state) and not ctx.allowed_fact_ids and not ctx.upstream_only_allowed:
                raise ValueError("EMPTY_MEMORY_NOT_AUTHORIZED")
            payload = memory_payload(ctx)
            if unified_memory or ctx.phase == "FINAL" or size == 1 or (request("MEMORY", payload)[1] <= cfg.max_review_input_tokens
                                                    and memory_fits(payload["working_memory"])):
                break
            size = max(1, size // 2)
        persist_memory_context(runtime, ctx)
        data = payload
        for attempt in range(cfg.max_protocol_retries + 1):
            raw = await complete("MEMORY" if attempt == 0 else "MEMORY_REPAIR", data)
            try:
                apply_memory(runtime, parse_memory(raw, ctx), ctx)
                return
            except ValueError as exc:
                error = str(exc)
                if isinstance(exc, MemberBranchLimit):
                    raise
                if "STALE" in error or "CONTEXT_CONSUMED" in error:
                    # A repair has no power to make a stale snapshot authoritative.
                    raise
                data = dict(original_memory_request=payload, validation_errors=error, rejected_response=raw)
        raise V52ProtocolError("MEMORY_PROTOCOL_ERROR:" + error)

    async def recall(job):
        if len(job.payload["candidates"]) > cfg.max_recall_candidates:
            raise V52ResourceLimit("RECALL_CANDIDATE_BUDGET")
        start = job.payload["offset"]
        batch = job.payload["candidates"][start:start + cfg.candidate_batch_size]
        # A deferred fact is recalled against the effective member instances,
        # not the unresolved plan template. The job itself is only runnable
        # when every upstream dependency is effective, so these targets are
        # fully concrete (possibly one per compatible member branch).
        query_instance = _query_projection_row(
            runtime.state,
            job.target_query,
            instantiate_members=True,
        )
        payload = dict(question=sample.question, query_instance=query_instance,
            candidate_batch=[], selectable_fact_ids=batch,
            fact_only=runtime.state.fact_only,
            member_bindings=isinstance(runtime.state.plan, EvidencePlanR2),
            context_id="RC" + digest([job.job_id, start, runtime.state.state_revision]))
        while True:
            payload["candidate_batch"] = [runtime.state.facts[f].model_dump() for f in batch]
            payload["selectable_fact_ids"] = batch
            if len(batch) <= 1 or request("RECALL", payload)[1] <= cfg.max_input_tokens:
                break
            batch = batch[:max(1, len(batch) // 2)]
        for _ in range(cfg.max_protocol_retries + 1):
            raw = await complete("RECALL", payload)
            try:
                selected = parse_selection(raw).selected_fact_ids
                if not set(selected) <= set(batch):
                    raise ValueError("RECALL_ID_NOT_SELECTABLE")
                runtime.finish_recall_batch(job.job_id, selected, batch_size=len(batch), expected_offset=start)
                return
            except ValueError as exc:
                if "STALE" in str(exc):
                    raise
                payload["validation_errors"] = str(exc)
        raise V52ProtocolError("SCAN_INCOMPLETE")

    active_job = None

    async def drain():
        nonlocal active_job
        rounds, sessions = 0, set()
        while (job := next_job(runtime.state)) is not None:
            if rounds >= cfg.max_memory_rounds_per_window:
                raise V52ResourceLimit("MEMORY_ROUND_BUDGET")
            if job.review_id and runtime.state.reviews[job.review_id].mode == "REBIND":
                sessions.add(job.review_id)
                if len(sessions) > cfg.max_rebind_sessions_per_window:
                    raise V52ResourceLimit("REBIND_SESSION_BUDGET")
            active_job = job.job_id
            job = runtime.claim(active_job)
            if job.kind == "CONTEXT_EXPAND":
                try:
                    runtime.expand_context(job.job_id)
                except ValueError as exc:
                    if str(exc) == "CONTEXT_EXPANSION_BUDGET":
                        raise V52ResourceLimit(str(exc)) from exc
                    raise
            elif job.kind == "RECALL":
                if unified_memory:
                    raise ValueError("UNIFIED_MEMORY_RECALL_JOB_FORBIDDEN")
                await recall(job)
            else:
                await review(job)
            rounds += 1
            active_job = None

    if runtime.state.status == "RUNNING":
        try:
            if plain_chunks and runtime.state.pending_chunk is not None:
                await update(chunk=runtime.state.pending_chunk)
            elif runtime.state.pending_window:
                await update(runtime.state.pending_window)
            await repair_plan()
            await drain()
            while not cursor.exhausted:
                if cursor.window_index >= cfg.max_windows:
                    raise V52ResourceLimit("WINDOW_BUDGET")
                if plain_chunks:
                    chunk = cursor.next_chunk()
                    runtime.record_chunk(chunk, cursor.state())
                    await update(chunk=runtime.state.pending_chunk)
                else:
                    window = cursor.next_anchored_window(cfg.chunk_size)
                    runtime.record_window(window, cursor.state())
                    await update(list(window.source_refs))
                await repair_plan()
                await drain()
            runtime.close_scope()
            await drain()
        except sqlite3.Error:
            raise
        except Exception as exc:
            if "STALE" in str(exc):
                # Reload the winner before writing diagnostics after a CAS race.
                runtime.state = StateR2.model_validate(store.latest_v52_state(run_id))
                staged = runtime.state.model_copy(deep=True)
                runtime.commit(staged, [("STALE_RESULT_REJECTED", {"reason": str(exc), "job_id": active_job})])
            runtime.fail(str(exc), job_id=active_job, resource=isinstance(exc, (V52ResourceLimit, MemberBranchLimit)))

    pack = None
    try:
        pack = budgeted_evidence_pack(runtime, counter)
        # ANSWER still reasons over the complete working-memory view.  It is
        # never used as a recovery shortcut after an unfinished runtime flow.
        if runtime.state.answer is None and runtime.state.status == "RUNNING":
            raw = await complete("ANSWER", dict(question=sample.question, working_memory=pack.model_dump(mode="json"),
                                                 answer_contract=cfg.answer_format,
                                                 fact_only=runtime.state.fact_only,
                                                 member_bindings=isinstance(runtime.state.plan, EvidencePlanR2)))
            value = extract_boxed(raw) if cfg.answer_format == "boxed" else raw.strip()
            if not value:
                raise ValueError("EMPTY_ANSWER")
            answer = dict(answer=None if value == "UNKNOWN" else value, answer_type=None, source_refs=[], raw_response=raw)
            staged = runtime.state.model_copy(deep=True)
            staged.answer = answer
            if staged.status == "RUNNING":
                staged.status = "INSUFFICIENT" if answer["answer"] is None else "ANSWERED"
            runtime.commit(staged, [("ANSWER_GENERATED", {"answer": answer})])
    except sqlite3.Error:
        raise
    except Exception as exc:
        runtime.fail(str(exc), resource=isinstance(exc, V52ResourceLimit))
    events = store.list_runtime_events(run_id)
    manifests = store.list_context_manifests(run_id)
    completions = {m["call_id"]: m for m in manifests if m.get("kind") == "MODEL_RESPONSE"}
    requests = [{**m, **{key: value for key, value in completions.get(m.get("call_id"), {}).items()
                         if key in {"memory_input_tokens", "memory_output_tokens", "finish_reason", "token_source"}}}
                for m in manifests if m.get("kind") == "MODEL_REQUEST"]
    from .replay import replay_events
    result = dict(run_id=run_id, protocol_version=PROTOCOL, memory_contract=CONTRACT, plan_format="subqueries",
        state=runtime.export(), status=runtime.state.status, reason_codes=runtime.state.reason_codes, answer=runtime.state.answer,
        raw_answer=(runtime.state.answer or {}).get("raw_response"), windows_processed=cursor.window_index, windows=cursor.window_index,
        streaming_protocol_valid=runtime.state.status in {"ANSWERED", "INSUFFICIENT"},
        evidence_pack=pack.model_dump(mode="json") if pack else None,
        answer_context_source_refs=[r.source_ref for r in pack.raw_evidence] if pack else [],
        source_ref_map={r.source_ref: archive.anchor(r.source_ref).original_source_ref for r in pack.raw_evidence} if pack else {},
        events=[e.model_dump(mode="json") for e in events], context_manifests=manifests,
        logical_model_calls=logical_calls, interface_calls=dict(Counter(m["interface"] for m in requests)), config=metadata)
    from .metrics_r2 import r2_metrics
    result["r2_metrics"] = r2_metrics(runtime.state, pack, result["events"], requests,
                                       model_calls=store.list_model_calls(run_id))
    if plain_chunks:
        result["r2_metrics"].update(chunk_count=cursor.window_index,
                                    chunk_body_tokens=len(cursor.tokens))
    if cfg.plan_repair_failure_policy == "continue_valid_plan":
        result["r2_metrics"].update(plan_repair_rejected_count=len(runtime.state.plan_repair_failures),
                                    plan_repair_rejected_basis=sorted(runtime.state.plan_repair_failures),
                                    plan_repair_success_count=sum(e["event_type"] == "PLAN_REPAIRED" for e in result["events"]),
                                    plan_repair_skipped_same_basis=repair_skips,
                                    plan_repair_continued=bool(runtime.state.plan_repair_failures)
                                    and runtime.state.status in {"ANSWERED", "INSUFFICIENT"})
    result["r2_metrics"]["transaction_replay_consistency"] = float(replay_events(events).export() == runtime.export())
    return result
