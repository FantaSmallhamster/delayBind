"""Pure event-log projection used for CPU-only offline replay."""

from __future__ import annotations

from typing import Iterable

from .query_graph import compile_open_query_graph
from .schema import Claim, QueryEdgeStatus, QueryPlan, RuntimeEvent, RuntimeStatus
from .runtime import RuntimeState


def replay_events(events: Iterable[RuntimeEvent]) -> RuntimeState:
    ordered = sorted(events, key=lambda event: event.event_seq or 0)
    plan_event = next((event for event in ordered if event.event_type == "PLAN_CREATED"), None)
    if plan_event is None:
        raise ValueError("event log has no PLAN_CREATED event")
    plan = QueryPlan.model_validate(plan_event.payload["plan"])
    graph = (
        plan_event.payload.get("query_graph")
        if plan_event.payload.get("query_graph") is not None
        else compile_open_query_graph(plan).model_dump(mode="json")
    )
    from .schema import OpenQueryGraph

    state = RuntimeState(plan=plan, query_graph=OpenQueryGraph.model_validate(graph))
    if plan_event.payload.get("edge_status"):
        state.edge_status = {
            edge_id: QueryEdgeStatus(status)
            for edge_id, status in plan_event.payload["edge_status"].items()
        }
    else:
        by_node = {node.id: node for node in state.query_graph.nodes}
        for edge in state.query_graph.edges:
            variables = sum(
                by_node[node_id].symbol.startswith("?")
                for node_id in (edge.subject_node_id, edge.object_node_id)
            )
            state.edge_status[edge.id] = (
                QueryEdgeStatus.ACTIVE if variables <= 1 else QueryEdgeStatus.DORMANT
            )
    for event in ordered:
        payload = event.payload
        if event.event_type == "ENTITY_BOUND":
            state.bindings[payload["variable"]] = payload["value"]
        elif event.event_type == "QUERY_VARIABLE_CANDIDATE_REGISTERED":
            variable = payload["variable"]
            candidate = {key: value for key, value in payload.items() if key != "variable"}
            state.binding_candidates.setdefault(variable, []).append(candidate)
        elif event.event_type == "QUERY_VARIABLE_BOUND":
            state.binding_provenance[payload["variable"]] = {
                "source_ref": payload["source_ref"],
                "stream_position": payload["stream_position"],
            }
        elif event.event_type == "QUERY_EDGE_ACTIVATED":
            state.edge_status[payload["edge_id"]] = QueryEdgeStatus.ACTIVE
        elif event.event_type == "QUERY_EDGE_DORMANT":
            state.edge_status[payload["edge_id"]] = QueryEdgeStatus.DORMANT
        elif event.event_type == "QUERY_EDGE_SATISFIED":
            state.edge_status[payload["edge_id"]] = QueryEdgeStatus.SATISFIED
            state.edge_support.setdefault(payload["edge_id"], []).append(payload["claim_id"])
        elif event.event_type == "QUERY_EDGE_CONFLICTED":
            state.edge_status[payload["edge_id"]] = QueryEdgeStatus.CONFLICTED
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
