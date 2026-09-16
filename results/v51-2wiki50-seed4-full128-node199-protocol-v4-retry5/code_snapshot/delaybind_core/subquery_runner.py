"""Two-agent V5.1 streaming runner: read, maintain, recall, verify, answer."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from .agent_prompts import (final_answer_prompt, lookup_prompt, memory_prompt,
                            plan_prompt, query_verify_prompt, reading_prompt, reading_repair_prompt)
from .agents import HighLevelAgent, LowLevelAgent
from .api import OpenAICompatibleClient
from .archive import RawArchive
from .cursor import ReadCursor, TextReadCursor, TextTokenizer
from .data import CanonicalSample, build_manifest
from .fact_protocol import (FactUpdate, MemoryUpdate, ProtocolError, parse_judgments, parse_memory,
                            parse_plan, parse_selection, parse_update, normalize_memory_queries)
from .manifest import Manifest
from .plan_validation import PlanValidationError, ensure_valid_plan
from .schema import AnswerResponse, QueryPlan, RuntimeEvent, RuntimeStatus
from .storage import SQLiteEventStore
from .subqueries import SubqueryRuntime
from .source_refs import VisibleSources

if TYPE_CHECKING:
    from .runner import RunnerConfig


class MemoryBudgetExceeded(RuntimeError):
    pass


def parse_final_answer(raw: str, answer_format: str) -> AnswerResponse:
    if answer_format == "json":
        return AnswerResponse.model_validate_json(raw)
    if answer_format == "text":
        if not raw.strip():
            raise ProtocolError("empty final answer")
        return AnswerResponse(answer=raw.strip())
    start = raw.rfind("\\boxed{")
    if start < 0:
        raise ProtocolError("boxed answer missing")
    start += len("\\boxed{")
    depth = 1
    for index in range(start, len(raw)):
        depth += (raw[index] == "{") - (raw[index] == "}")
        if depth == 0:
            return AnswerResponse(answer=raw[start:index].strip())
    raise ProtocolError("unclosed boxed answer")


async def run_subqueries(
    *, client: OpenAICompatibleClient, config: RunnerConfig,
    run_id: str, sample: CanonicalSample, store: SQLiteEventStore,
    manifest: Manifest | None = None, plan: QueryPlan | None = None,
    reader_client: OpenAICompatibleClient | None = None, tokenizer: TextTokenizer | None = None,
) -> dict[str, Any]:
    from .runner import ModelBudgetExceeded

    calls = 0
    by_role: Counter[str] = Counter()
    by_interface: Counter[str] = Counter()
    protocol_failures: list[dict[str, Any]] = []
    runtime: SubqueryRuntime | None = None
    unit = "tokens" if tokenizer else "characters"
    if config.memory_token_budget is not None and tokenizer is None:
        raise ValueError("memory_token_budget requires the model tokenizer; pass tokenizer or tokenizer_path")
    memory_limit = config.memory_token_budget if tokenizer else config.memory_char_budget
    if tokenizer and memory_limit is None:
        memory_limit = 8192

    def log(event_type: str, payload: dict[str, Any]) -> None:
        store.append_runtime_event(RuntimeEvent(run_id=run_id, event_id=str(uuid4()), event_type=event_type, payload=payload))

    async def invoke(agent_client: OpenAICompatibleClient, role: str, interface: str, prompt: str,
                     *, parser: Callable[[str], Any], retries: int | None = None,
                     schema: dict[str, Any] | None = None,
                     on_invalid: Callable[[], Any] | None = None,
                     repair_prompt: Callable[[str, Exception], str] | None = None) -> Any:
        nonlocal calls
        for attempt in range((config.max_response_retries if retries is None else retries) + 1):
            if calls >= config.max_model_calls:
                raise ModelBudgetExceeded(f"max_model_calls={config.max_model_calls}")
            calls += 1
            by_role[role] += 1
            by_interface[interface] += 1
            log("AGENT_CALLED", {"role": role, "interface": interface, "attempt": attempt + 1})
            raw = await agent_client.complete(
                run_id=run_id, interface=interface, agent_role=role,
                messages=[{"role": "user", "content": prompt}], response_schema=schema,
            )
            try:
                if not isinstance(raw, str):
                    raise ProtocolError("model response must contain text")
                return parser(raw)
            except (ValueError, ValidationError, PlanValidationError) as exc:
                log("AGENT_RESPONSE_INVALID", {"role": role, "interface": interface, "error": str(exc)})
                if attempt >= (config.max_response_retries if retries is None else retries):
                    if on_invalid is not None:
                        diagnostic = {"role": role, "interface": interface, "error": str(exc)}
                        protocol_failures.append(diagnostic)
                        log("PROTOCOL_REPAIR_EXHAUSTED", diagnostic)
                        return on_invalid()
                    raise
                if repair_prompt is not None:
                    prompt = repair_prompt(raw, exc)
                else:
                    prompt += f"\nYour previous output was invalid: {exc}\nReturn a corrected complete response in the specified format."
        raise AssertionError("unreachable")

    high, low = HighLevelAgent(client, invoke), LowLevelAgent(reader_client or client, invoke)
    if plan is None:
        def valid_plan(raw: str) -> QueryPlan:
            candidate = parse_plan(raw)
            ensure_valid_plan(candidate)
            return candidate
        plan = await high.call("PLAN", plan_prompt(sample.question), parser=valid_plan, retries=config.max_plan_retries)
    ensure_valid_plan(plan)
    archive = RawArchive(store, run_id, session_prefix=True)
    runtime = SubqueryRuntime(run_id=run_id, plan=plan, archive=archive, store=store,
                              defer_unbound=config.defer_unbound)
    if manifest is not None:
        cursor = ReadCursor(manifest, archive)
    elif sample.context is not None:
        cursor = TextReadCursor(sample.context, archive, sample_id=sample.sample_id,
                                dataset_id=sample.metadata.get("dataset_id", "text"), tokenizer=tokenizer)
    else:
        manifest = build_manifest(sample)
        cursor = ReadCursor(manifest, archive)
    windows = 0
    memory_signature: str | None = None
    previously_accepted: set[str] = set()

    def queries() -> list[dict[str, Any]]:
        return [runtime.state.query_projection(query.id) for query in runtime.state.plan.queries]

    def measure(text: str) -> int:
        return len(tokenizer.encode(text)) if tokenizer else len(text)

    def memory_text(*, final: bool = False) -> str:
        text = runtime.memory_text(visible_only=not final)
        if not final and memory_limit is not None:
            pinned = {fid for execution in runtime.state.executions.values() for fid in execution.support_fact_ids}
            ids = list(runtime.state.active_fact_ids)
            while measure(text) > memory_limit:
                removable = next((fid for fid in reversed(ids) if fid not in pinned), None)
                if removable is None:
                    break
                ids.remove(removable)
                runtime.set_visible(ids)
                log("MEMORY_FACT_HIDDEN", {"fact_id": removable, "reason": "VIEW_BUDGET"})
                text = runtime.memory_text()
        size = measure(text)
        log("MEMORY_VIEW_MEASURED", {"size": size, "unit": unit, "limit": memory_limit, "final": final})
        if memory_limit is not None and size > memory_limit:
            raise MemoryBudgetExceeded("FINAL_MEMORY_BUDGET" if final else "BINDING_SUPPORT_MEMORY_BUDGET")
        return text

    def apply_memory(response: MemoryUpdate, *, eof: bool) -> bool:
        changed = False
        def command(name: str, action: Callable[[], Any]) -> Any:
            nonlocal changed
            try:
                result = action()
                changed = changed or bool(result)
                return result
            except (ValueError, KeyError, PlanValidationError) as exc:
                log("MEMORY_COMMAND_REJECTED", {"command": name, "error": str(exc)})
                return None
        if response.patches:
            command("PATCH", lambda: runtime.patch_queries(response.patches, window_index=windows))
        for qid, fids in response.routes.items():
            command("ROUTE", lambda qid=qid, fids=fids: runtime.route_candidates(qid, fids, window_index=windows))
        for qid, fids in response.retract.items():
            command("RETRACT", lambda qid=qid, fids=fids: runtime.retract(qid, fids, window_index=windows))
        for qid in response.unbind:
            def unbind(qid=qid):
                runtime.invalidate({qid} | runtime.descendants(qid), reason="HIGH_AGENT_UNBOUND", window_index=windows)
                runtime._activate_ready(window_index=windows, from_binding=False)
            command("UNBIND", unbind)
        for qid, refs in response.clear_conflicts.items():
            def clear(qid=qid, refs=refs):
                runtime.resolve_support(qid, refs)
                runtime._set_query(qid, conflicts=[])
                log("QUERY_CONFLICT_CLEARED", {"query_id": qid, "support_refs": refs})
            command("CLEAR_CONFLICT", clear)
        proposals: dict[str, list[Any]] = defaultdict(list)
        for proposal in response.bindings:
            proposals[proposal.query_id].append(proposal)
        for qid, candidates in proposals.items():
            execution = runtime.state.executions.get(qid)
            if execution is not None and (execution.status == "DORMANT" or execution.review_pending):
                # A proposal cannot skip the low-level deferred review. Ask
                # for a fresh supported binding after activation processing.
                log("BINDING_HELD", {"query_id": qid, "reason": "DEPENDENCY_OR_REVIEW_PENDING"})
                continue
            values = {json.dumps(item.value, sort_keys=True, ensure_ascii=False) for item in candidates}
            if len(values) > 1:
                if qid in runtime.state.executions:
                    runtime._set_query(qid, conflicts=["INCOMPATIBLE_BINDING_PROPOSALS"])
                log("BINDING_CONFLICT", {"query_id": qid})
                continue
            proposal = candidates[0].model_copy(update={"support_refs": list(dict.fromkeys(
                ref for item in candidates for ref in item.support_refs))})
            command("BIND", lambda proposal=proposal: runtime.apply_binding(proposal, window_index=windows, eof=eof))
        command("KEEP", lambda: runtime.set_visible(response.keep if response.keep is not None else
                [fid for fid in runtime.state.active_fact_ids if fid in runtime.state.accepted_ids()]))
        return changed

    async def maintain(*, eof: bool) -> bool:
        nonlocal memory_signature, previously_accepted
        accepted = runtime.state.accepted_ids()
        runtime.set_visible(list(dict.fromkeys([fid for fid in runtime.state.active_fact_ids if fid in accepted]
                                               + sorted(accepted - previously_accepted))))
        previously_accepted = accepted
        view = memory_text()
        signature = json.dumps([queries(), view, runtime.state.hints, eof], sort_keys=True, ensure_ascii=False)
        if signature == memory_signature:
            return False
        memory_signature = signature
        latest = MemoryUpdate()

        def memory_response(raw: str) -> MemoryUpdate:
            nonlocal latest
            latest = normalize_memory_queries(parse_memory(raw, recover=True), set(runtime.state.executions))
            for ignored in latest.ignored_lines:
                log("MEMORY_LINE_IGNORED", ignored)
            for rejected in latest.rejected_lines:
                log("MEMORY_LINE_REJECTED", rejected)
            if latest.rejected_lines:
                raise ProtocolError("; ".join(item["reason"] for item in latest.rejected_lines[:4]))
            return latest

        response = await high.call("MEMORY", memory_prompt(sample.question, queries(), view,
                                   hints=runtime.state.hints, eof=eof), parser=memory_response,
                                   on_invalid=lambda: latest)
        return apply_memory(response, eof=eof)

    def binding_support(qid: str) -> str:
        facts = {fid for dep in runtime.dependency_ids(qid)
                 for fid in runtime.state.executions[dep].support_fact_ids}
        from .agent_prompts import facts_view
        return facts_view([runtime.state.facts[fid].model_dump(mode="json") for fid in sorted(facts)])

    async def process_activations(*, eof: bool) -> None:
        queue = runtime.drain_activations()
        while queue:
            for qid in queue:
                execution = runtime.state.executions[qid]
                if execution.status != "ACTIVE" or not execution.review_pending:
                    continue
                if not config.enable_defer_callback:
                    log("DEFER_CALLBACK_DISABLED", {"query_id": qid})
                    runtime.finish_review(qid)
                    continue
                candidates = runtime.candidate_facts(qid)
                log("DEFER_WORKSPACE_LOOKUP", {"query_id": qid, "fact_ids": [fact.fact_id for fact in candidates]})
                if len(candidates) > config.max_verify_candidates:
                    raise MemoryBudgetExceeded("MAX_VERIFY_CANDIDATES")
                selected: set[str] = set()
                # Scan the entire known bucket. No top-k truncation or early
                # binding is allowed before later batches have been examined.
                for start in range(0, len(candidates), config.candidate_batch_size):
                    batch = candidates[start:start + config.candidate_batch_size]
                    allowed = {fact.fact_id for fact in batch}
                    context_ids = {fid for dep in runtime.dependency_ids(qid)
                                   for fid in runtime.state.executions[dep].support_fact_ids}

                    def selection(raw: str, allowed=allowed, context_ids=context_ids) -> set[str]:
                        ignored: set[str] = set()
                        found = parse_selection(raw, allowed, context_ids=context_ids, ignored_context=ignored)
                        if ignored:
                            log("RECALL_CONTEXT_IDS_IGNORED", {"query_id": qid, "fact_ids": sorted(ignored)})
                        return found

                    found = await low.call("RECALL", lookup_prompt(sample.question, runtime.state.query_projection(qid),
                                           [fact.model_dump(mode="json") for fact in batch], binding_support(qid)),
                                           parser=selection, on_invalid=set)
                    selected.update(found)
                log("DEFER_CANDIDATES_SELECTED", {"query_id": qid, "fact_ids": sorted(selected), "scanned": len(candidates)})
                chosen = [fact for fact in candidates if fact.fact_id in selected]
                judgments: dict[str, str] = {}
                format_failed_ids: set[str] = set()
                for start in range(0, len(chosen), config.candidate_batch_size):
                    batch = chosen[start:start + config.candidate_batch_size]
                    batch_ids = {fact.fact_id for fact in batch}
                    # Source restoration happens only AFTER deferred selection.
                    raw_sources = {entry.source_ref: entry for fact in batch for ref in fact.source_refs
                                   for entry in archive.fetch(ref, neighborhood=config.verify_source_neighborhood)}
                    log("DEFER_SOURCES_FETCHED", {"query_id": qid, "fact_ids": sorted(batch_ids),
                                                  "source_refs": sorted(raw_sources)})
                    def unresolved_verification(batch_ids=batch_ids):
                        format_failed_ids.update(batch_ids)
                        return {fact_id: "UNCERTAIN" for fact_id in batch_ids}

                    verdicts = await low.call("VERIFY", query_verify_prompt(
                        sample.question, runtime.state.query_projection(qid),
                        [fact.model_dump(mode="json") for fact in batch],
                        [entry.model_dump(mode="json") for entry in raw_sources.values()], binding_support(qid)),
                        parser=lambda raw, batch_ids=batch_ids: parse_judgments(raw, batch_ids),
                        on_invalid=unresolved_verification)
                    judgments.update(verdicts)
                if selected:
                    runtime.apply_reviews(qid, judgments, selected_ids=selected, format_failed_ids=format_failed_ids)
                runtime.finish_review(qid)
            # The high-level agent may resolve an intermediate selection from
            # existing dependency evidence, even when the defer bucket is empty.
            # This is memory maintenance, never an extra VERIFY invocation.
            await maintain(eof=eof)
            queue = runtime.drain_activations()

    raw_answer: str | None = None
    answer: AnswerResponse | None = None
    failure: str | None = None
    try:
        while not cursor.exhausted:
            if windows >= config.max_windows:
                raise MemoryBudgetExceeded("MAX_WINDOWS")
            window = cursor.next_window(token_budget=config.chunk_size)
            if window is None:
                break
            visible = memory_text()
            visible_refs = {ref for fact in runtime.state.memory(visible_only=True)["facts"] for ref in fact["source_refs"]}
            sources = VisibleSources([
                *[entry.model_dump(mode="json") for entry in window.entries],
                *[archive.entry(ref).model_dump(mode="json") for ref in sorted(visible_refs)],
            ])
            buffered = FactUpdate()
            fact_keys: set[tuple] = set()
            hint_keys: set[tuple] = set()
            rejected_lines: list[dict[str, str]] = []

            def reading(raw: str):
                rejected_lines.clear()
                response = parse_update(raw, recover=True)
                for ignored in response.ignored_lines:
                    log("UPDATE_LINE_IGNORED", ignored)
                if response.bindings:
                    response.rejected_lines.append({"line": "BIND", "reason": "the low-level reader cannot BIND; return facts only"})
                normalized, mappings = sources.normalize_update(response, query_ids=set(runtime.state.executions), recover=True)
                for fact in normalized.facts:
                    key = (fact.query_id, tuple(sorted(fact.source_refs)), fact.text, fact.relevance)
                    if key not in fact_keys:
                        fact_keys.add(key)
                        buffered.facts.append(fact)
                for hint in normalized.hints:
                    key = (tuple(sorted(hint.source_refs)), hint.text)
                    if key not in hint_keys:
                        hint_keys.add(key)
                        buffered.hints.append(hint)
                if mappings:
                    log("SOURCE_REFERENCES_NORMALIZED", {"mappings": mappings, "window_index": windows})
                if normalized.query_id_mappings:
                    log("QUERY_IDS_NORMALIZED", {"mappings": normalized.query_id_mappings})
                for rejected in normalized.rejected_lines:
                    log("UPDATE_LINE_REJECTED", rejected)
                rejected_lines.extend(normalized.rejected_lines)
                if normalized.rejected_lines:
                    errors = "; ".join(item["reason"] for item in normalized.rejected_lines[:4])
                    raise ProtocolError(f"{errors}. Omit facts with no supporting source. Citable source IDs: {', '.join(sources.labels())}")
                return buffered

            def repair_reading(raw: str, error: Exception) -> str:
                rejected = rejected_lines or [{"line": str(raw), "reason": str(error)}]
                log("UPDATE_REPAIR_REQUESTED", {"rejected_lines": rejected,
                    "retained_facts": len(buffered.facts), "window_index": windows})
                return reading_repair_prompt(sample.question, queries(), list(sources.entries.values()),
                    rejected_lines=rejected, memory=visible, source_refs=sources.labels())

            update = await low.call("UPDATE", reading_prompt(sample.question, queries(),
                                    [entry.model_dump(mode="json") for entry in window.entries], memory=visible,
                                    source_refs=sources.labels()), parser=reading, on_invalid=lambda: buffered,
                                    repair_prompt=repair_reading)
            committed = runtime.ingest(update, window_index=windows,
                                        allowed_refs={entry.source_ref for entry in window.entries} | visible_refs)
            eof = cursor.exhausted
            if committed or update.hints or (eof and any(query.requires_complete_set for query in runtime.state.plan.queries)):
                await maintain(eof=eof)
                await process_activations(eof=eof)
            windows += 1
            if config.snapshot_every_windows > 0 and windows % config.snapshot_every_windows == 0:
                events = store.list_runtime_events(run_id)
                store.save_snapshot(run_id, f"window-{windows:06d}", events[-1].event_seq or 0, {
                    "run_id": run_id, "window_index": windows, "cursor_position": cursor.position,
                    "state": runtime.state.export(), "logical_model_calls": calls,
                })
    except (ModelBudgetExceeded, MemoryBudgetExceeded) as exc:
        failure = str(exc)
        runtime.set_status(RuntimeStatus.RESOURCE_LIMIT, [failure])

    # No query-completion or semantic sufficiency gate precedes ANSWER.
    # Even empty/incomplete working memory is passed through when budget allows.
    final_format = config.answer_format
    if final_format == "auto":
        final_format = "boxed" if sample.context is not None else "json"
    pack = runtime.evidence_pack()
    try:
        final_memory = memory_text(final=True)
        def final(raw: str) -> AnswerResponse:
            nonlocal raw_answer
            parsed = parse_final_answer(raw, final_format)
            permitted = {ref for fact in pack["facts"] for ref in fact["source_refs"]}
            if parsed.source_refs:
                sources = VisibleSources(archive.entry(ref).model_dump(mode="json") for ref in sorted(permitted))
                parsed.source_refs = list(dict.fromkeys(ref for value in parsed.source_refs for ref in sources.resolve(value)))
            if not set(parsed.source_refs) <= permitted:
                raise ProtocolError("ANSWER cited a source outside working memory")
            raw_answer = raw
            return parsed
        answer = await high.call("ANSWER", final_answer_prompt(sample.question, final_memory, answer_format=final_format),
                                 parser=final, schema=AnswerResponse.model_json_schema() if final_format == "json" else None)
        if failure is None:
            runtime.set_status(RuntimeStatus.ANSWERED if answer.answer is not None else RuntimeStatus.INSUFFICIENT,
                               [] if answer.answer is not None else ["ANSWER_MODEL_ABSTAINED"])
    except (ModelBudgetExceeded, MemoryBudgetExceeded) as exc:
        failure = str(exc)
        runtime.set_status(RuntimeStatus.RESOURCE_LIMIT, [failure])
    pack["answer_value"] = answer.answer if answer else None
    pack["answer_type"] = answer.answer_type if answer else None
    return {
        "run_id": run_id, "question": sample.question, "plan_format": "subqueries", "protocol_version": "v5.1",
        "windows_processed": windows, "chunk_size": config.chunk_size,
        "window_budget_unit": getattr(cursor, "budget_unit", "words"),
        "streaming_protocol_valid": windows >= config.min_streaming_windows,
        "input_exhausted": cursor.exhausted, "logical_model_calls": calls,
        "agent_calls": dict(by_role), "interface_calls": dict(by_interface),
        "state": runtime.state.export(), "evidence_pack": pack,
        "answer": answer.model_dump(mode="json") if answer else None,
        "raw_answer": raw_answer, "answer_format": final_format,
        "protocol_valid": not protocol_failures,
        "protocol_repair_failures": protocol_failures,
        "unresolved_queries": [qid for qid, execution in runtime.state.executions.items() if execution.status != "RESOLVED"],
    }
