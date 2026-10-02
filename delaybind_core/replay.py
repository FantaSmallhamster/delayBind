"""Pure event-log projection for offline V08 replay."""
from __future__ import annotations
from typing import Iterable
from .schema import QueryPlan, RuntimeEvent
from .subqueries import SubqueryState, replay_subqueries


def replay_events(events: Iterable[RuntimeEvent]) -> SubqueryState:
    ordered = sorted(events, key=lambda event: event.event_seq or 0)
    plan_event = next((event for event in reversed(ordered) if event.event_type == "PLAN_CREATED"), None)
    if plan_event is None:
        raise ValueError("event log has no PLAN_CREATED event")
    plan = QueryPlan.model_validate(plan_event.payload["plan"])
    return replay_subqueries(
        [event for event in ordered if (event.event_seq or 0) >= (plan_event.event_seq or 0)], plan,
    )
