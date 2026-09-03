"""Pure event-log projection used for CPU-only offline replay."""

from __future__ import annotations

from typing import Iterable

from .schema import Claim, QueryPlan, RuntimeEvent, RuntimeStatus
from .runtime import RuntimeState


def replay_events(events: Iterable[RuntimeEvent]) -> RuntimeState:
    ordered = sorted(events, key=lambda event: event.event_seq or 0)
    plan_event = next((event for event in ordered if event.event_type == "PLAN_CREATED"), None)
    if plan_event is None:
        raise ValueError("event log has no PLAN_CREATED event")
    state = RuntimeState(plan=QueryPlan.model_validate(plan_event.payload["plan"]))
    for event in ordered:
        payload = event.payload
        if event.event_type == "ENTITY_BOUND":
            state.bindings[payload["variable"]] = payload["value"]
        elif event.event_type == "CLAIM_DEFERRED":
            claim = Claim.model_validate(payload["claim"])
            state.deferred[claim.claim_id] = claim
        elif event.event_type == "CLAIM_COMMITTED":
            claim = Claim.model_validate(payload["claim"])
            if payload.get("verification_required", False):
                state.pending[claim.claim_id] = claim
            else:
                state.verified[claim.claim_id] = claim
        elif event.event_type == "CLAIM_PROMOTED":
            claim = Claim.model_validate(payload["claim"])
            state.deferred.pop(claim.claim_id, None)
            state.pending.pop(claim.claim_id, None)
            state.verified[claim.claim_id] = claim
        elif event.event_type == "VERIFY_REJECTED":
            claim = Claim.model_validate(payload["claim"])
            state.deferred.pop(claim.claim_id, None)
            state.pending.pop(claim.claim_id, None)
            state.verified.pop(claim.claim_id, None)
            state.rejected[claim.claim_id] = claim
        elif event.event_type == "VERIFY_CONFLICT":
            claim = Claim.model_validate(payload["claim"])
            state.conflicts.setdefault(claim.claim_id, []).append(payload.get("reason") or "VERIFY_CONFLICT")
        elif event.event_type == "OPERATOR_EXECUTED":
            state.operator_trace.append(dict(payload))
        elif event.event_type == "RUN_STATUS":
            state.status = RuntimeStatus(payload["status"])
            state.reason_codes = list(payload.get("reason_codes", []))
    return state
