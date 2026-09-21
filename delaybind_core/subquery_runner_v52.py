"""V5.2 UPDATE → unified MEMORY → RECALL queue; no independent VERIFY."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from uuid import uuid4

from .agent_prompts_v52 import PROMPT_VERSION, messages
from .agents_v52 import HighLevelAgentV52, LowLevelAgentV52
from .archive import SentenceArchive
from .cursor_v52 import SentenceReadCursor
from .evidence_context import build_answer_context, build_memory_context, build_update_context
from .fact_protocol_v52 import UpdateV52, UpdateRepairScope, parse_update
from .navigation_graph import query_projection
from .schema import AnswerResponse
from .schema_v52 import QueryPlanV3, digest, use_key
from .subqueries_v52 import SubqueryRuntime, add_queue, current_uses
from .token_budget import TokenCounter
from .text_protocol_v52 import parse_plan as parse_text_plan, parse_memory, parse_selection
from .text_views_v52 import memory_view, raw_view


class V52ResourceLimit(RuntimeError):
    pass


class V52ProtocolError(RuntimeError):
    pass


def extract_boxed(text):
    index = text.rfind("\\boxed{")
    if index < 0:
        raise ValueError("ANSWER_BOX_MISSING")
    start, depth = index + 7, 1
    for end in range(start, len(text)):
        if text[end] == "{":
            depth += 1
        elif text[end] == "}":
            depth -= 1
        if depth == 0:
            if text[end + 1:].strip():
                raise ValueError("ANSWER_BOX_NOT_LAST")
            return text[start:end]
    raise ValueError("ANSWER_BOX_UNCLOSED")


async def run_subqueries_v52(runner, *, run_id, sample, manifest, store, plan=None):
    cfg = runner.config
    # Preserve the existing V5.1 public input forms and auto-format convention.
    if cfg.answer_format == "auto":
        cfg = replace(cfg, answer_format="boxed")
    if manifest is None and (cfg.sentence_splitting or sample.context is None):
        from .data import build_manifest
        from .sentence_index import text_manifest
        manifest = (text_manifest(sample.context, sample_id=sample.sample_id) if sample.context is not None
                    else build_manifest(sample))
    if plan is not None and not isinstance(plan, QueryPlanV3):
        raise ValueError("PLAN_PROTOCOL_MISMATCH:v5.2 requires schema v3")
    counter = TokenCounter(runner.tokenizer, encoding_name=cfg.tokenizer_encoding)
    reader_client = runner.reader_client or runner.client
    high, low = HighLevelAgentV52(runner.client), LowLevelAgentV52(reader_client)
    archive = SentenceArchive(store, run_id)
    saved = store.latest_v52_state(run_id)
    if saved and plan is None:
        plan = QueryPlanV3.model_validate(saved["plan"])
    logical_calls = sum(m.get("kind") == "MODEL_REQUEST" for m in store.list_context_manifests(run_id))

    def request(interface, payload):
        msg = messages(interface, payload)
        return msg, counter.serialized({"messages": msg})

    def limit_for(interface):
        return (cfg.max_review_input_tokens if interface.startswith("MEMORY") else
                cfg.max_answer_input_tokens if interface == "ANSWER" else cfg.max_input_tokens)

    def memory_fits(memory):
        if cfg.memory_token_budget is not None:
            return counter.count(memory_view(memory)) <= cfg.memory_token_budget
        return len(memory_view(memory)) <= cfg.memory_char_budget

    async def complete(interface, payload):
        nonlocal logical_calls
        reserve = 0 if interface == "ANSWER" else cfg.answer_reserve_calls
        if logical_calls >= cfg.max_model_calls - reserve:
            raise V52ResourceLimit("MODEL_CALL_BUDGET")
        msg, tokens = request(interface, payload)
        memory = (payload["original_memory_request"].get("working_memory")
                  if interface == "MEMORY_REPAIR" else payload.get("working_memory"))
        if memory is not None and not memory_fits(memory):
            raise V52ResourceLimit("FINAL_RAW_MEMORY_BUDGET" if interface == "ANSWER" else "MEMORY_CONTENT_BUDGET")
        client = high.client if interface in high.interfaces else low.client
        output_budget = getattr(getattr(client, "config", None), "max_tokens", cfg.answer_reserve_tokens)
        if interface == "ANSWER":
            output_budget = max(output_budget, cfg.answer_reserve_tokens)
        if tokens > limit_for(interface) or tokens + output_budget > cfg.max_context_tokens:
            raise V52ResourceLimit("FINAL_RAW_MEMORY_BUDGET" if interface == "ANSWER" else f"{interface}_INPUT_BUDGET")
        logical_calls += 1
        call_id = "CALL-" + str(uuid4())
        raw_bundle = payload.get("working_memory", {})
        if interface in {"UPDATE", "UPDATE_REPAIR"}:
            raw_bundle = payload.get("window_sources", []) + payload.get("working_memory", {}).get("raw_evidence", [])
        if interface == "MEMORY_REPAIR":
            raw_bundle = payload["original_memory_request"]["working_memory"]
        raw_items = raw_bundle.get("raw_evidence", []) if isinstance(raw_bundle, dict) else raw_bundle
        store.save_context_manifest(run_id, call_id, {
            "kind": "MODEL_REQUEST", "context_id": call_id, "memory_context_id": payload.get("context_id"),
            "interface": interface, "prompt_version": PROMPT_VERSION, "prompt_hash": digest(msg),
            "input_tokens": tokens, "output_token_budget": output_budget, "tokenizer": counter.identity,
            "raw_bundle_hash": digest(raw_items),
            "raw_input_tokens": counter.count(raw_view(raw_items)) if raw_items else 0,
            "visible_source_refs": [raw["source_ref"] for raw in raw_items],
        })
        agent = high if interface in high.interfaces else low
        return await agent.call(interface, run_id=run_id, messages=msg, response_schema=None,
                                extra={"max_tokens": output_budget})

    if plan is None:
        error = None
        for attempt in range(cfg.max_plan_retries + 1):
            raw = await complete("PLAN", {"question": sample.question, "validation_errors": error})
            try:
                candidate = parse_text_plan(raw)
                if not isinstance(candidate, QueryPlanV3):
                    raise ValueError("PLAN_PROTOCOL_MISMATCH")
                plan = candidate
                break
            except ValueError as exc:
                error = str(exc)
        if plan is None:
            raise V52ProtocolError(f"PLAN_PROTOCOL_ERROR:{error}")
    runtime = SubqueryRuntime(run_id=run_id, plan=plan, archive=archive, store=store,
                              callback=cfg.enable_defer_callback, defer_unbound=cfg.defer_unbound)
    metadata = {"protocol_version": "v5.2", "prompt_version": PROMPT_VERSION,
                "question_hash": digest(sample.question),
                "manifest_hash": digest(manifest.model_dump(mode="json") if manifest is not None else sample.context),
                "config": cfg.protocol_mapping(), "tokenizer": counter.identity,
                "high_model": getattr(getattr(runner.client, "config", None), "model", type(runner.client).__name__),
                "low_model": getattr(getattr(reader_client, "config", None), "model", type(reader_client).__name__)}
    if runtime.state.run_metadata and runtime.state.run_metadata != metadata:
        raise ValueError("RESUME_CONFIGURATION_CHANGED")
    if not runtime.state.run_metadata:
        staged = runtime.state.model_copy(deep=True)
        staged.run_metadata = metadata
        runtime.commit(staged, [("RUN_CONFIGURED", metadata)])
    if cfg.sentence_splitting:
        cursor = SentenceReadCursor(manifest, archive, counter=counter, window_mode=cfg.window_mode,
                                    state=runtime.state.cursor_state or None)
    else:
        from .cursor_legacy_v52 import LegacyReadCursor
        cursor = LegacyReadCursor(archive, text=sample.context, sample_id=sample.sample_id, manifest=manifest,
                                  tokenizer=runner.tokenizer, state=runtime.state.cursor_state or None)

    def set_failure(code, *, resource=False):
        staged = runtime.state.model_copy(deep=True)
        staged.status = "RESOURCE_LIMIT" if resource else "RUNTIME_ERROR"
        if code not in staged.reason_codes:
            staged.reason_codes.append(code)
        runtime.commit(staged, [("RUN_STATUS", {"status": staged.status, "reason_codes": staged.reason_codes})])

    def memory_payload(context):
        return {"question": sample.question, **context.model_dump(mode="json")}

    async def review(qid):
        batch_size = cfg.candidate_batch_size
        while True:
            context = build_memory_context(runtime, qid, batch_size=batch_size,
                                           before=cfg.source_context_before, after=cfg.source_context_after)
            payload = memory_payload(context)
            _, tokens = request("MEMORY", payload)
            if (tokens <= cfg.max_review_input_tokens and memory_fits(payload["working_memory"])) or batch_size == 1:
                break
            batch_size = max(1, batch_size // 2)
        rejected, error = None, None
        for attempt in range(cfg.max_protocol_retries + 1):
            interface = "MEMORY" if attempt == 0 else "MEMORY_REPAIR"
            data = payload if attempt == 0 else {
                "context_id": context.context_id, "validation_errors": error,
                "rejected_response": rejected, "original_memory_request": payload,
            }
            raw = await complete(interface, data)
            try:
                proposal = parse_memory(raw, context.context_id, plan=runtime.state.plan)
                runtime.apply_memory(proposal, context)
                return
            except ValueError as exc:
                error, rejected = str(exc), raw
                if "STALE_CONTEXT" in error or "STALE_STATE_REVISION" in error:
                    # Do not try to repair authority with the old context_id.
                    return
        raise V52ProtocolError(f"MEMORY_PROTOCOL_ERROR:{error}")

    def expand_context():
        staged = runtime.state.model_copy(deep=True)
        changed = False
        for key, req in list(staged.context_requests.items()):
            qid = req["query_id"]
            use = staged.uses.get(use_key(qid, staged.executions[qid].input_signature, req["fact_id"]))
            if use is None or use.status != "HELD":
                staged.context_requests.pop(key, None)
                continue
            raw = archive.fetch_bounded_context(req["anchor"], before=req["before"], after=req["after"])
            old = set(staged.extra_context_refs.get(qid, [])) | set(use.reviewed_source_refs)
            new = {r.source_ref for r in raw} - old
            if not new:
                continue
            if staged.context_expansions >= cfg.max_context_expansions:
                raise V52ResourceLimit("CONTEXT_EXPANSION_BUDGET")
            staged.context_expansions += 1
            staged.extra_context_refs[qid] = sorted(old | new)
            use.status = "PENDING"
            add_queue(staged, qid)
            changed = True
        if changed:
            runtime.commit(staged, [("RAW_CONTEXT_EXPANDED", {"expansions": staged.context_expansions})])
        return changed

    async def scan(job):
        if len(job.candidate_ids) > cfg.max_recall_candidates:
            raise V52ResourceLimit("RECALL_CANDIDATE_BUDGET")
        ex = runtime.state.executions[job.query_id]
        if ex.input_signature != job.input_signature or ex.version != job.query_version:
            staged = runtime.state.model_copy(deep=True)
            staged.recall_jobs[job.job_id].stage = "INVALIDATED"
            runtime.commit(staged, [("STALE_RESULT_REJECTED", {"job_id": job.job_id})])
            return
        batch = job.candidate_ids[job.scan_offset:job.scan_offset + cfg.candidate_batch_size]
        query = next(q for q in query_projection(runtime.state)["queries"] if q["id"] == job.query_id)
        payload = {"question": sample.question, "query_instance": query,
                   "candidate_batch": [runtime.state.facts[f].model_dump(mode="json") for f in batch],
                   "selectable_fact_ids": batch}
        while len(batch) > 1 and request("RECALL", payload)[1] > cfg.max_input_tokens:
            batch = batch[:max(1, len(batch) // 2)]
            payload["candidate_batch"] = [runtime.state.facts[f].model_dump(mode="json") for f in batch]
            payload["selectable_fact_ids"] = batch
        error, selection = None, None
        for attempt in range(cfg.max_protocol_retries + 1):
            if error:
                payload["validation_errors"] = error
            raw = await complete("RECALL", payload)
            try:
                selection = parse_selection(raw)
                if not set(selection.selected_fact_ids) <= set(batch):
                    raise ValueError("RECALL_ID_NOT_SELECTABLE")
                break
            except ValueError as exc:
                selection, error = None, str(exc)
        staged = runtime.state.model_copy(deep=True)
        target = staged.recall_jobs[job.job_id]
        if selection is None:
            target.stage = "SCAN_INCOMPLETE"
            runtime.commit(staged, [("SCAN_INCOMPLETE", {"job_id": job.job_id, "error": error})])
            raise V52ProtocolError("SCAN_INCOMPLETE")
        target.selected_fact_ids = sorted(set(target.selected_fact_ids) | set(selection.selected_fact_ids))
        target.scan_offset += len(batch)
        if target.scan_offset == len(target.candidate_ids):
            target.stage = "REVIEW" if target.selected_fact_ids else "DONE"
            for fid in target.selected_fact_ids:
                staged.uses[use_key(job.query_id, job.input_signature, fid)].status = "PENDING"
            add_queue(staged, job.query_id)
            staged.executions[job.query_id].review_barrier = bool(target.selected_fact_ids)
        runtime.commit(staged, [("RECALL_BATCH_SCANNED", {"job_id": job.job_id, "candidate_ids": batch,
                                                        "selected_fact_ids": selection.selected_fact_ids})])

    async def drain_work_queue():
        rounds = 0
        while True:
            expand_context()
            if rounds >= cfg.max_memory_rounds_per_window:
                if runtime.state.review_queue or any(j.stage == "SCAN" for j in runtime.state.recall_jobs.values()):
                    raise V52ResourceLimit("MEMORY_ROUND_BUDGET")
                break
            # All candidate scans finish before any associated MEMORY can bind.
            job = next((j for j in runtime.state.recall_jobs.values() if j.stage == "SCAN"), None)
            if job:
                await scan(job)
                rounds += 1
                continue
            qid = next((q for q in runtime.state.review_queue if runtime.state.executions[q].status != "DORMANT"), None)
            if qid is None:
                break
            await review(qid)
            rounds += 1

    async def update_window(refs):
        payload = build_update_context(runtime, sample.question, refs)
        visible_refs = payload["visible_sources"]
        retained, rejected = [], None
        repair_scope, original_rejected = None, None
        for attempt in range(cfg.max_protocol_retries + 1):
            interface = "UPDATE" if attempt == 0 else "UPDATE_REPAIR"
            data = payload if attempt == 0 else {
                **payload, "rejected_items": original_rejected, "retained_items": retained,
                "repair_targets": [dict(t) for t in repair_scope.targets], "repair_errors": rejected,
            }
            raw = await complete(interface, data)
            try:
                update, rejected = parse_update(raw, query_ids=runtime.state.executions, visible_sources=visible_refs)
            except ValueError as exc:
                update, rejected = UpdateV52(facts=[], hints=[]), [{"item": raw, "error": str(exc)}]
            if repair_scope is not None:
                update, scope_errors = repair_scope.restrict(update)
                rejected.extend(scope_errors)
            elif rejected:
                original_rejected = rejected
                repair_scope = UpdateRepairScope(original_rejected)
            runtime.ingest_all(update, visible_sources=visible_refs, window_index=archive.watermark)
            retained.extend([f.model_dump() for f in update.facts])
            retained.extend([{"kind": "hint", **h.model_dump()} for h in update.hints])
            if not rejected:
                runtime.finish_window()
                return
        raise V52ProtocolError("UPDATE_REPAIR_EXHAUSTED")

    if runtime.state.status == "RUNNING":
        try:
            if runtime.state.pending_window:
                await update_window(runtime.state.pending_window)
            await drain_work_queue()
            while not cursor.exhausted:
                if cursor.window_index >= cfg.max_windows:
                    raise V52ResourceLimit("WINDOW_BUDGET")
                window = cursor.next_anchored_window(cfg.chunk_size)
                runtime.record_window(window, cursor.state())
                await update_window(list(window.source_refs))
                await drain_work_queue()
            runtime.close_scope()
            await drain_work_queue()
        except V52ResourceLimit as exc:
            set_failure(str(exc), resource=True)
        except (ValueError, V52ProtocolError) as exc:
            set_failure(str(exc))
        except Exception as exc:
            # Database faults must propagate: never report an uncommitted state.
            import sqlite3
            if isinstance(exc, sqlite3.Error):
                raise
            set_failure(f"API_OR_RUNTIME_ERROR:{type(exc).__name__}:{exc}")

    final_pack = None
    try:
        final_pack = build_answer_context(runtime)
        if runtime.state.answer is None:
            payload = {"question": sample.question, "working_memory": final_pack.model_dump(mode="json"),
                       "answer_contract": cfg.answer_format}
            raw = await complete("ANSWER", payload)
            if cfg.answer_format == "json":
                answer = AnswerResponse.model_validate_json(raw).model_dump(mode="json")
                if not set(answer["source_refs"]) <= {r.source_ref for r in final_pack.raw_evidence}:
                    raise ValueError("ANSWER_SOURCE_NOT_VISIBLE")
                answer["raw_response"] = raw
            else:
                value = extract_boxed(raw) if cfg.answer_format == "boxed" else raw.strip()
                answer = {"answer": None if value == "UNKNOWN" else value, "answer_type": None,
                          "source_refs": [], "raw_response": raw}
            staged = runtime.state.model_copy(deep=True)
            staged.answer = answer
            if staged.status == "RUNNING":
                staged.status = "ANSWERED" if answer["answer"] is not None else "INSUFFICIENT"
            runtime.commit(staged, [("ANSWER_GENERATED", {"answer": answer})])
    except V52ResourceLimit as exc:
        set_failure(str(exc), resource=True)
    except Exception as exc:
        import sqlite3
        if isinstance(exc, sqlite3.Error):
            raise
        set_failure(f"ANSWER_ERROR:{exc}")
    events = [e.model_dump(mode="json") for e in store.list_runtime_events(run_id)]
    call_manifests = store.list_context_manifests(run_id)
    source_map = {}
    if final_pack:
        source_map = {r.source_ref: archive.anchor(r.source_ref).original_source_ref for r in final_pack.raw_evidence}
    result = {
        "run_id": run_id, "protocol_version": "v5.2", "plan_format": "subqueries", "state": runtime.export(),
        "raw_answer": (runtime.state.answer or {}).get("raw_response"),
        "windows_processed": cursor.window_index,
        "streaming_protocol_valid": runtime.state.status in {"ANSWERED", "INSUFFICIENT"},
        "status": runtime.state.status, "reason_codes": runtime.state.reason_codes,
        "answer": runtime.state.answer, "evidence_pack": final_pack.model_dump(mode="json") if final_pack else None,
        "answer_context_source_refs": [r.source_ref for r in final_pack.raw_evidence] if final_pack else [],
        "source_ref_map": source_map, "events": events, "context_manifests": call_manifests,
        "interface_calls": dict(Counter(m["interface"] for m in call_manifests if m.get("kind") == "MODEL_REQUEST")),
        "logical_model_calls": logical_calls, "windows": cursor.window_index, "config": metadata,
    }
    from .metrics_v52 import raw_first_metrics
    result["v52_metrics"] = raw_first_metrics(runtime.state, final_pack, events)
    from .replay import replay_events
    result["v52_metrics"]["transaction_replay_consistency"] = float(
        replay_events(store.list_runtime_events(run_id)).export() == runtime.export())
    result["v52_metrics"]["raw_tokens_per_call"] = [
        {"interface": m["interface"], "tokens": m["raw_input_tokens"]}
        for m in call_manifests if m.get("kind") == "MODEL_REQUEST"]
    return result
