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
from .manifest import Manifest
from .prompts import answer_prompt, plan_prompt, update_prompt, verify_prompt
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
    QueryPlan,
    TripleEvent,
    UpdateResponse,
    VerifyResult,
    VerifyStatus,
    RuntimeStatus,
)
from .storage import SQLiteEventStore


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
                                sample.question, schema=plan_schema, correction=correction
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
                issues = validate_plan(candidate_plan, question=sample.question)
                if not any(issue.fatal for issue in issues):
                    plan = candidate_plan
                    break
                correction = format_plan_issues(issues)
                if attempt >= self.config.max_plan_retries:
                    raise PlanValidationError(issues)
            if plan is None:
                raise PlanValidationError([PlanIssue("PLAN_MISSING", "no valid plan returned")])
        else:
            ensure_valid_plan(plan, question=sample.question)
        runtime = EvidenceRuntime(
            run_id=run_id,
            plan=plan,
            archive=archive,
            store=store,
            verify_committed=self.config.verify_committed,
            defer_unbound=self.config.defer_unbound,
        )
        cursor = ReadCursor(manifest, archive)
        update_schema = UpdateResponse.model_json_schema()
        windows = 0
        resource_limited = False
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
            verification_queue = [
                claim
                for result in results
                for claim in (*result.deferred_matches, *result.verification_candidates)
            ]
            seen_claim_ids: set[str] = set()
            while verification_queue and len(seen_claim_ids) < self.config.max_verify_candidates:
                claim = verification_queue.pop(0)
                if claim.claim_id in seen_claim_ids:
                    continue
                seen_claim_ids.add(claim.claim_id)
                neighborhood = archive.fetch(claim.evidence_assertions[0].source_ref, neighborhood=1)
                verify_schema = VerifyResult.model_json_schema()
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
                    verify_result = VerifyResult.model_validate_json(raw_verify)
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
                        verify_result = VerifyResult.model_validate_json(raw_verify)
                except ModelBudgetExceeded:
                    runtime.state.status = RuntimeStatus.RESOURCE_LIMIT
                    runtime.state.reason_codes.append("MAX_MODEL_CALLS")
                    resource_limited = True
                    runtime._emit("RUN_STATUS", {"status": runtime.state.status.value, "reason_codes": runtime.state.reason_codes})
                    break
                runtime.apply_verification(verify_result)
                verification_queue.extend(runtime.drain_verification_matches())
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
                pack.answer_value = answer.answer
                pack.answer_type = answer.answer_type
        return {
            "run_id": run_id,
            "question": sample.question,
            "state": runtime.state.export(),
            "evidence_pack": pack.model_dump(mode="json") if pack is not None else None,
        }


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.from_json(path)
