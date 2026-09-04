"""Training-free V5 runner over the stateless API client.

This runner intentionally keeps model calls separate from the deterministic
core. It is suitable for small API experiments; the ReMemR1/verl adapter can
reuse the same prompt and response parsing functions later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .api import OpenAICompatibleClient
from .archive import RawArchive
from .cursor import ReadCursor
from .data import CanonicalSample
from .profiler import infer_answer_contract
from .manifest import Manifest
from .prompts import answer_prompt, plan_prompt, targeted_update_prompt, update_prompt, verify_prompt
from .plan_validation import (
    PlanIssue,
    PlanValidationError,
    ensure_valid_plan,
    format_plan_issues,
    validate_plan,
)
from .runtime import EvidenceRuntime
from .schema import (
    AnswerResponse,
    EdgeFillResponse,
    QueryPlan,
    TripleEvent,
    VerifyDecision,
    UpdateResponse,
    VerifyResult,
    VerifyStatus,
    RuntimeStatus,
)
from .storage import SQLiteEventStore
from .text_match import contains_normalized_span


@dataclass(frozen=True)
class RunnerConfig:
    chunk_size: int = 5000
    answer_mode: str = "runtime"
    max_windows: int = 100000
    max_verify_candidates: int = 1000
    max_plan_retries: int = 1
    max_model_calls: int = 1000
    snapshot_every_windows: int = 1
    verify_committed: bool = True
    max_graph_claims: int = 128
    defer_unbound: bool = True
    max_verify_expansions: int = 1
    verify_expansion_limit: int = 32
    require_evidence_sources: bool = True
    min_streaming_windows: int = 0
    query_graph_mode: str = "open"
    require_source_span: bool = True
    callback_retrieval_limit: int = 16
    max_targeted_updates: int = 16


class ModelBudgetExceeded(RuntimeError):
    """Raised when a run reaches its configured logical model-call budget."""


class V5Runner:
    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        config: RunnerConfig | None = None,
    ):
        self.client = client
        self.config = config or RunnerConfig()

    async def run(
        self,
        *,
        run_id: str,
        sample: CanonicalSample,
        manifest: Manifest,
        store: SQLiteEventStore,
        plan: QueryPlan | None = None,
    ) -> dict[str, Any]:
        archive = RawArchive(store, run_id)
        logical_model_calls = 0

        async def complete(**kwargs: Any) -> str:
            nonlocal logical_model_calls
            if logical_model_calls >= self.config.max_model_calls:
                raise ModelBudgetExceeded(
                    f"max_model_calls={self.config.max_model_calls} reached for run {run_id}"
                )
            logical_model_calls += 1
            return await self.client.complete(**kwargs)

        target_free_answer = self.config.answer_mode == "evidence"

        def prepare_plan(candidate: QueryPlan) -> QueryPlan:
            if not target_free_answer:
                return candidate
            contract = infer_answer_contract(sample)
            constraints = {
                **candidate.constraints,
                "answer_contract_source": "QUESTION_PROFILE",
                "answer_target_mode": "EVIDENCE_MODEL",
            }
            return candidate.model_copy(
                update={"answer_contract": contract, "constraints": constraints}
            )

        if plan is None:
            plan_schema = QueryPlan.model_json_schema()
            correction: str | None = None
            for attempt in range(self.config.max_plan_retries + 1):
                raw_plan = await complete(
                    run_id=run_id,
                    interface="PLAN",
                    messages=[
                        {
                            "role": "user",
                            "content": plan_prompt(
                                sample.question,
                                schema=plan_schema,
                                correction=correction,
                                require_answer_target=not target_free_answer,
                            ),
                        }
                    ],
                    response_schema=plan_schema,
                )
                try:
                    candidate_plan = QueryPlan.model_validate_json(raw_plan)
                except ValidationError as exc:
                    issues = [
                        PlanIssue(
                            "PLAN_SCHEMA_INVALID",
                            f"model output does not match QueryPlan schema: {exc}",
                            fatal=True,
                        )
                    ]
                    correction = format_plan_issues(issues)
                    if attempt >= self.config.max_plan_retries:
                        raise PlanValidationError(issues) from exc
                    continue
                candidate_plan = prepare_plan(candidate_plan)
                issues = validate_plan(
                    candidate_plan,
                    question=sample.question,
                    require_answer_contract=not target_free_answer,
                    require_answer_target=not target_free_answer,
                )
                if not any(issue.fatal for issue in issues):
                    plan = candidate_plan
                    break
                correction = format_plan_issues(issues)
                if attempt >= self.config.max_plan_retries:
                    raise PlanValidationError(issues)
            if plan is None:
                raise PlanValidationError([PlanIssue("PLAN_MISSING", "no valid plan returned")])
        else:
            plan = prepare_plan(plan)
            ensure_valid_plan(
                plan,
                question=sample.question,
                require_answer_contract=not target_free_answer,
                require_answer_target=not target_free_answer,
            )
        runtime = EvidenceRuntime(
            run_id=run_id,
            plan=plan,
            archive=archive,
            store=store,
            verify_committed=self.config.verify_committed,
            defer_unbound=self.config.defer_unbound,
            execute_operators=True,
            query_graph_mode=self.config.query_graph_mode,
            require_source_span=self.config.require_source_span,
        )
        cursor = ReadCursor(manifest, archive)
        update_schema = UpdateResponse.model_json_schema()
        edge_fill_schema = EdgeFillResponse.model_json_schema()
        verify_schema = VerifyDecision.model_json_schema()
        windows = 0
        resource_limited = False
        targeted_attempted: set[tuple[str, str]] = set()
        targeted_updates = 0

        def parse_verify_decision(
            raw: str,
            claim_id: str,
            neighborhood_entries: list[Any],
        ) -> VerifyResult:
            try:
                decision = VerifyDecision.model_validate_json(raw)
            except ValidationError as exc:
                return VerifyResult(
                    claim_id=claim_id,
                    status=VerifyStatus.REJECT,
                    reason=f"VERIFY_SCHEMA_INVALID:{exc.errors()[0]['type']}",
                )
            if decision.claim_id != claim_id:
                raise ValueError(
                    f"VERIFY returned claim_id {decision.claim_id!r} for candidate {claim_id!r}"
                )
            if decision.status == VerifyStatus.ACCEPT:
                by_ref = {entry.source_ref: entry for entry in neighborhood_entries}
                invalid_refs = sorted(set(decision.supporting_source_refs) - set(by_ref))
                supporting_text = decision.supporting_text or ""
                cited_text = [
                    by_ref[source_ref].text
                    for source_ref in decision.supporting_source_refs
                    if source_ref in by_ref
                ]
                if invalid_refs or not supporting_text.strip() or not any(
                    contains_normalized_span(text, supporting_text) for text in cited_text
                ):
                    reason = "INVALID_VERIFY_EVIDENCE: " + (
                        "unknown source_ref" if invalid_refs else "supporting_text not found in cited source"
                    )
                    return VerifyResult(claim_id=decision.claim_id, status=VerifyStatus.REJECT, reason=reason)
            return VerifyResult(
                claim_id=decision.claim_id,
                status=decision.status,
                reason=decision.reason,
                expanded_source_refs=decision.supporting_source_refs,
                supporting_text=decision.supporting_text,
            )

        async def targeted_candidates(
            edge_id: str,
            entries: list[Any],
            *,
            trigger: str,
        ) -> list[Any]:
            nonlocal targeted_updates, resource_limited
            if edge_id not in runtime.active_edge_ids():
                return []
            fresh = [
                entry
                for entry in entries
                if (edge_id, entry.source_ref) not in targeted_attempted
            ][: self.config.callback_retrieval_limit]
            if not fresh or targeted_updates >= self.config.max_targeted_updates:
                return []
            targeted_updates += 1
            targeted_attempted.update((edge_id, entry.source_ref) for entry in fresh)
            runtime._emit(
                "TARGETED_UPDATE_REQUESTED",
                {
                    "edge_id": edge_id,
                    "trigger": trigger,
                    "source_refs": [entry.source_ref for entry in fresh],
                },
            )
            try:
                raw = await complete(
                    run_id=run_id,
                    interface="UPDATE",
                    messages=[
                        {
                            "role": "user",
                            "content": targeted_update_prompt(
                                sample.question,
                                runtime.edge_projection(edge_id),
                                [entry.model_dump(mode="json") for entry in fresh],
                                schema=update_schema,
                            ),
                        }
                    ],
                    response_schema=update_schema,
                )
            except ModelBudgetExceeded:
                runtime.state.status = RuntimeStatus.RESOURCE_LIMIT
                runtime.state.reason_codes.append("MAX_MODEL_CALLS")
                resource_limited = True
                return []
            callback_events: list[TripleEvent] = []
            try:
                legacy_update = UpdateResponse.model_validate_json(raw)
            except ValidationError as update_exc:
                # Some OpenAI-compatible gateways/cache entries still return
                # the compact callback envelope. Accept it as a compatibility
                # path, while Runtime continues to enforce source/span/edge
                # matching deterministically.
                try:
                    edge_fill = EdgeFillResponse.model_validate_json(raw)
                except ValidationError as edge_exc:
                    # A callback is a recall enhancement, never a prerequisite
                    # for preserving already verified state. Treat malformed
                    # output as empty and leave an auditable trace.
                    runtime._emit(
                        "TARGETED_UPDATE_SCHEMA_INVALID",
                        {
                            "edge_id": edge_id,
                            "trigger": trigger,
                            "error": str(update_exc),
                            "compact_error": str(edge_exc),
                        },
                    )
                    return []
                allowed_refs = {entry.source_ref for entry in fresh}
                edge_projection = runtime.edge_projection(edge_id)
                callback_events.extend(
                    TripleEvent(
                        event_id=f"edge-fill-{index}",
                        source_ref=match.source_ref,
                        subject=match.subject,
                        concrete_relation=str(edge_projection["relation"]),
                        matched_family=str(edge_projection["relation"]),
                        object=match.object,
                        pattern_hint=edge_id,
                        span_hint=match.supporting_text,
                    )
                    for index, match in enumerate(edge_fill.matches)
                )
            else:
                allowed_refs = {entry.source_ref for entry in fresh}
                callback_events = [
                    event.model_copy(update={"pattern_hint": edge_id})
                    for event in legacy_update.events
                    if event.source_ref in allowed_refs
                ]
            update = UpdateResponse(events=callback_events)
            runtime._emit(
                "TARGETED_UPDATE_COMPLETED",
                {"edge_id": edge_id, "event_count": len(update.events), "trigger": trigger},
            )
            results = runtime.apply_events(update.events)
            return [
                claim
                for result in results
                for claim in (*result.deferred_matches, *result.verification_candidates)
            ]

        async def verify_candidates(initial: list[Any]) -> None:
            nonlocal resource_limited
            verification_queue = list(initial)
            seen_claim_ids: set[str] = set()
            while verification_queue and len(seen_claim_ids) < self.config.max_verify_candidates:
                claim = verification_queue.pop(0)
                if claim.claim_id in seen_claim_ids:
                    continue
                seen_claim_ids.add(claim.claim_id)
                neighborhood = archive.fetch(claim.evidence_assertions[0].source_ref, neighborhood=1)
                try:
                    raw_verify = await complete(
                        run_id=run_id,
                        interface="VERIFY",
                        messages=[
                            {
                                "role": "user",
                                "content": verify_prompt(
                                    sample.question,
                                    claim.model_dump(mode="json"),
                                    [entry.model_dump(mode="json") for entry in neighborhood],
                                    schema=verify_schema,
                                ),
                            }
                        ],
                        response_schema=verify_schema,
                    )
                    verify_result = parse_verify_decision(raw_verify, claim.claim_id, neighborhood)
                    for _ in range(self.config.max_verify_expansions):
                        if verify_result.status != VerifyStatus.NEED_MORE_CONTEXT:
                            break
                        expanded = archive.expanded_context(
                            claim.evidence_assertions[0].source_ref,
                            mentions=[str(claim.subject), str(claim.object)],
                            limit=self.config.verify_expansion_limit,
                        )
                        if {entry.source_ref for entry in expanded} == {
                            entry.source_ref for entry in neighborhood
                        }:
                            break
                        raw_verify = await complete(
                            run_id=run_id,
                            interface="VERIFY",
                            messages=[
                                {
                                    "role": "user",
                                    "content": verify_prompt(
                                        sample.question,
                                        claim.model_dump(mode="json"),
                                        [entry.model_dump(mode="json") for entry in expanded],
                                        schema=verify_schema,
                                    ),
                                }
                            ],
                            response_schema=verify_schema,
                        )
                        verify_result = parse_verify_decision(raw_verify, claim.claim_id, expanded)
                except ModelBudgetExceeded:
                    runtime.state.status = RuntimeStatus.RESOURCE_LIMIT
                    runtime.state.reason_codes.append("MAX_MODEL_CALLS")
                    resource_limited = True
                    return
                runtime.apply_verification(verify_result)
                verification_queue.extend(runtime.drain_verification_matches())
                for activated_edge_id in runtime.drain_activated_edges():
                    anchors = runtime.edge_anchor_values(activated_edge_id)
                    recalled = archive.search_mentions(
                        anchors, limit=self.config.callback_retrieval_limit
                    )
                    verification_queue.extend(
                        await targeted_candidates(
                            activated_edge_id,
                            recalled,
                            trigger="BINDING_ACTIVATION",
                        )
                    )

        while not cursor.exhausted:
            if windows >= self.config.max_windows:
                runtime.state.status = RuntimeStatus.RESOURCE_LIMIT
                runtime.state.reason_codes.append("MAX_WINDOWS")
                resource_limited = True
                runtime._emit(
                    "RUN_STATUS",
                    {"status": runtime.state.status.value, "reason_codes": runtime.state.reason_codes},
                )
                break
            window = cursor.next_window(token_budget=self.config.chunk_size)
            if window is None:
                break
            runtime.register_read_window(
                windows, [entry.source_ref for entry in window.entries]
            )
            try:
                raw_update = await complete(
                    run_id=run_id,
                    interface="UPDATE",
                    messages=[
                        {
                            "role": "user",
                            "content": update_prompt(
                                sample.question,
                                plan,
                                runtime.state.graph_projection(max_claims=self.config.max_graph_claims),
                                [entry.model_dump(mode="json") for entry in window.entries],
                                schema=update_schema,
                            ),
                        }
                    ],
                    response_schema=update_schema,
                )
            except ModelBudgetExceeded as exc:
                runtime.state.status = RuntimeStatus.RESOURCE_LIMIT
                runtime.state.reason_codes.append("MAX_MODEL_CALLS")
                resource_limited = True
                runtime._emit("RUN_STATUS", {"status": runtime.state.status.value, "reason_codes": runtime.state.reason_codes})
                break
            update = UpdateResponse.model_validate_json(raw_update)
            results = runtime.apply_events(update.events)
            verification_candidates = [
                claim
                for result in results
                for claim in (*result.deferred_matches, *result.verification_candidates)
            ]
            await verify_candidates(verification_candidates)
            if resource_limited:
                break

            # A focused second pass protects ACTIVE root edges from sparse
            # general UPDATE omissions without scanning unrelated archive text.
            for edge_id in runtime.active_edge_ids():
                anchors = runtime.edge_anchor_values(edge_id)
                relevant_entries = [
                    entry
                    for entry in window.entries
                    if any(
                        contains_normalized_span(entry.text, anchor)
                        or " ".join(entry.title.casefold().split())
                        == " ".join(anchor.casefold().split())
                        for anchor in anchors
                    )
                ]
                candidates = await targeted_candidates(
                    edge_id,
                    relevant_entries,
                    trigger="ACTIVE_EDGE_CURRENT_WINDOW",
                )
                await verify_candidates(candidates)
                if resource_limited:
                    break
            windows += 1
            if (
                self.config.snapshot_every_windows > 0
                and windows % self.config.snapshot_every_windows == 0
            ):
                events = store.list_runtime_events(run_id)
                store.save_snapshot(
                    run_id,
                    snapshot_id=f"window-{windows:06d}",
                    event_seq=events[-1].event_seq or 0,
                    payload={
                        "run_id": run_id,
                        "manifest_id": manifest.manifest_id,
                        "window_index": windows,
                        "cursor_position": cursor.position,
                        "state": runtime.state.export(),
                        "logical_model_calls": logical_model_calls,
                    },
                )
            if resource_limited:
                break
        streaming_protocol_valid = windows >= self.config.min_streaming_windows
        if not resource_limited and not streaming_protocol_valid:
            runtime.state.status = RuntimeStatus.INSUFFICIENT
            runtime.state.reason_codes = [
                f"STREAMING_PROTOCOL_TOO_SHORT:{windows}<{self.config.min_streaming_windows}"
            ]
            runtime._emit(
                "STREAMING_PROTOCOL_REJECTED",
                {
                    "windows_processed": windows,
                    "min_streaming_windows": self.config.min_streaming_windows,
                },
            )
            runtime._emit(
                "RUN_STATUS",
                {"status": runtime.state.status.value, "reason_codes": runtime.state.reason_codes},
            )
            pack = None
        else:
            pack = None if resource_limited else runtime.finalize()
        if pack is not None and self.config.answer_mode == "evidence":
            answer_schema = AnswerResponse.model_json_schema()
            try:
                raw_answer = await complete(
                    run_id=run_id,
                    interface="ANSWER",
                    messages=[
                        {
                            "role": "user",
                            "content": answer_prompt(
                                sample.question,
                                pack.model_dump(mode="json"),
                                schema=answer_schema,
                            ),
                        }
                    ],
                    response_schema=answer_schema,
                )
            except ModelBudgetExceeded:
                runtime.state.status = RuntimeStatus.RESOURCE_LIMIT
                runtime.state.reason_codes.append("MAX_MODEL_CALLS")
                runtime._emit("RUN_STATUS", {"status": runtime.state.status.value, "reason_codes": runtime.state.reason_codes})
                pack = None
            if pack is not None:
                answer = AnswerResponse.model_validate_json(raw_answer)
                permitted_refs = {
                    assertion.source_ref
                    for claim in pack.claims
                    for assertion in claim.evidence_assertions
                }
                invalid_refs = sorted(set(answer.source_refs) - permitted_refs)
                missing_refs = self.config.require_evidence_sources and bool(pack.claims) and not answer.source_refs
                if answer.answer is None or invalid_refs or missing_refs:
                    runtime.state.status = RuntimeStatus.INSUFFICIENT
                    reasons = []
                    if answer.answer is None:
                        reasons.append("ANSWER_MODEL_ABSTAINED")
                    if invalid_refs:
                        reasons.append("ANSWER_UNSUPPORTED_SOURCE_REFS:" + ",".join(invalid_refs))
                    if missing_refs:
                        reasons.append("ANSWER_SOURCE_REFS_MISSING")
                    runtime.state.reason_codes = reasons
                    runtime._emit(
                        "ANSWER_REJECTED",
                        {"reason_codes": reasons, "answer": answer.model_dump(mode="json")},
                    )
                    runtime._emit(
                        "RUN_STATUS",
                        {"status": runtime.state.status.value, "reason_codes": reasons},
                    )
                    pack = None
                else:
                    pack.answer_value = answer.answer
                    pack.answer_type = answer.answer_type or pack.answer_type
                    runtime._emit(
                        "ANSWER_ACCEPTED",
                        {"answer": answer.model_dump(mode="json")},
                    )
        return {
            "run_id": run_id,
            "question": sample.question,
            "windows_processed": windows,
            "window_word_budget": self.config.chunk_size,
            "streaming_protocol_valid": streaming_protocol_valid,
            "state": runtime.state.export(),
            "evidence_pack": pack.model_dump(mode="json") if pack is not None else None,
        }


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.from_json(path)
