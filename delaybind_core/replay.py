"""Pure R2 event-log projection for offline replay."""

from __future__ import annotations

from typing import Iterable

from .schema import RuntimeEvent
from .schema_r2 import StateR2


def replay_events(events: Iterable[RuntimeEvent]) -> StateR2:
    return replay_r2(sorted(events, key=lambda event: event.event_seq or 0))


def replay_r2(events):
    """Only complete, contiguous R2 transactions are publishable."""
    from .schema_r2 import StateR2
    from .schema_v52 import digest
    from .navigation_r2 import assert_invariants
    groups, state, revision, run_id = {}, None, 0, None
    for event in events:
        if event.schema_version != "v5.2-r2" or not event.transaction_id:
            raise ValueError("MIXED_OR_UNVERSIONED_EVENT_LOG")
        if run_id is not None and event.run_id != run_id:
            raise ValueError("MIXED_RUN_EVENT_LOG")
        run_id = event.run_id
        group = groups.setdefault(event.transaction_id, [])
        if event.event_type != "R2_TRANSACTION_COMMITTED":
            group.append((event.event_type, event.payload))
            continue
        payload = event.payload
        if len(group) != payload["event_count"] or digest(group) != payload["events_digest"]:
            raise ValueError("INCOMPLETE_TRANSACTION")
        candidate = StateR2.model_validate(payload["state"])
        if candidate.state_revision != revision + 1:
            raise ValueError("NONCONTIGUOUS_TRANSACTION_LOG")
        assert_invariants(candidate)
        state, revision = candidate, candidate.state_revision
        del groups[event.transaction_id]
    if state is None:
        raise ValueError("NO_COMMITTED_R2_STATE")
    return state
