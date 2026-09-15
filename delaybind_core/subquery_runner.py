"""Two-agent V5.1 streaming runner: read, maintain, recall, verify, answer."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from .agent_prompts import (final_answer_prompt, lookup_prompt, memory_prompt,
                            plan_prompt, query_verify_prompt, reading_prompt)
from .agents import HighLevelAgent, LowLevelAgent
from .api import OpenAICompatibleClient
from .archive import RawArchive
from .cursor import ReadCursor, TextReadCursor, TextTokenizer
from .data import CanonicalSample, build_manifest
from .fact_protocol import (MemoryUpdate, ProtocolError, parse_judgments, parse_memory,
                            parse_plan, parse_selection, parse_update)
from .manifest import Manifest
from .plan_validation import PlanValidationError, ensure_valid_plan
from .schema import AnswerResponse, QueryPlan, RuntimeEvent, RuntimeStatus
from .storage import SQLiteEventStore
from .subqueries import SubqueryRuntime

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
                     schema: dict[str, Any] | None = None) -> Any:
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
                return parser(raw)
            except (ValueError, ValidationError, PlanValidationError) as exc:
                log("AGENT_RESPONSE_INVALID", {"role": role, "interface": interface, "error": str(exc)})
                if attempt >= (config.max_response_retries if retries is None else retries):
                    raise
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
        response = await high.call("MEMORY", memory_prompt(sample.question, queries(), view,
                                   hints=runtime.state.hints, eof=eof), parser=parse_memory)
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
                    found = await low.call("RECALL", lookup_prompt(sample.question, runtime.state.query_projection(qid),
                                           [fact.model_dump(mode="json") for fact in batch], binding_support(qid)),
                                           parser=lambda raw, allowed=allowed: parse_selection(raw, allowed))
                    selected.update(found)
                log("DEFER_CANDIDATES_SELECTED", {"query_id": qid, "fact_ids": sorted(selected), "scanned": len(candidates)})
                chosen = [fact for fact in candidates if fact.fact_id in selected]
                judgments: dict[str, str] = {}
                for start in range(0, len(chosen), config.candidate_batch_size):
                    batch = chosen[start:start + config.candidate_batch_size]
                    batch_ids = {fact.fact_id for fact in batch}
                    # Source restoration happens only AFTER deferred selection.
                    raw_sources = {entry.source_ref: entry for fact in batch for ref in fact.source_refs
                                   for entry in archive.fetch(ref, neighborhood=config.verify_source_neighborhood)}
                    log("DEFER_SOURCES_FETCHED", {"query_id": qid, "fact_ids": sorted(batch_ids),
                                                  "source_refs": sorted(raw_sources)})
                    verdicts = await low.call("VERIFY", query_verify_prompt(
                        sample.question, runtime.state.query_projection(qid),
                        [fact.model_dump(mode="json") for fact in batch],
                        [entry.model_dump(mode="json") for entry in raw_sources.values()], binding_support(qid)),
                        parser=lambda raw, batch_ids=batch_ids: parse_judgments(raw, batch_ids))
                    judgments.update(verdicts)
                if selected:
                    runtime.apply_reviews(qid, judgments, selected_ids=selected)
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
            def reading(raw: str):
                response = parse_update(raw)
                if response.bindings:
                    raise ProtocolError("the low-level reader cannot BIND; return facts only")
                return response
            update = await low.call("UPDATE", reading_prompt(sample.question, queries(),
                                    [entry.model_dump(mode="json") for entry in window.entries], memory=visible), parser=reading)
            visible_refs = {ref for fact in runtime.state.memory(visible_only=True)["facts"] for ref in fact["source_refs"]}
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
        "unresolved_queries": [qid for qid, execution in runtime.state.executions.items() if execution.status != "RESOLVED"],
    }
