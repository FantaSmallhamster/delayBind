"""Deterministic delayed-binding runtime.

The runtime treats model actions as proposals.  It owns disposition, binding,
deferred lookup, claim promotion and conflict bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .archive import FutureSourceAccessError, RawArchive
from .operators import OperatorError, execute_operator
from .schema import (
    Action,
    Claim,
    Disposition,
    EvidenceAssertion,
    EvidenceKind,
    EvidencePack,
    Pattern,
    OpenQueryGraph,
    QueryEdge,
    QueryEdgeStatus,
    QueryPlan,
    RuntimeEvent,
    RuntimeStatus,
    TripleEvent,
    VerifyResult,
    VerifyStatus,
)
from .storage import SQLiteEventStore
from .query_graph import compile_open_query_graph
from .text_match import contains_normalized_span


def _norm(value: Any) -> str:
    return " ".join(str(value).casefold().strip(" \t\r\n\"'“”‘’").split())


def _is_var(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("?")


def _canonical_event_id(event: TripleEvent) -> str:
    """Namespace model-local event labels by immutable factual content."""
    payload = {
        "source_ref": event.source_ref,
        "subject": event.subject,
        "relation": event.matched_family or event.concrete_relation,
        "object": event.object,
        "qualifiers": event.qualifiers,
        "polarity": event.polarity.value,
        "modality": event.modality.value,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    return f"event_{digest}"


@dataclass
class RuntimeState:
    plan: QueryPlan
    query_graph: OpenQueryGraph
    query_graph_mode: str = "open"
    bindings: dict[str, Any] = field(default_factory=dict)
    binding_provenance: dict[str, dict[str, Any]] = field(default_factory=dict)
    binding_candidates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    edge_status: dict[str, QueryEdgeStatus] = field(default_factory=dict)
    edge_support: dict[str, list[str]] = field(default_factory=dict)
    deferred_trigger_sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    deferred: dict[str, Claim] = field(default_factory=dict)
    pending: dict[str, Claim] = field(default_factory=dict)
    verified: dict[str, Claim] = field(default_factory=dict)
    rejected: dict[str, Claim] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    status: RuntimeStatus = RuntimeStatus.RUNNING
    reason_codes: list[str] = field(default_factory=list)
    operator_trace: list[dict[str, Any]] = field(default_factory=list)

    def export(self) -> dict[str, Any]:
        return {
            "plan": self.plan.model_dump(mode="json"),
            "query_graph": self.query_graph.model_dump(mode="json"),
            "query_graph_mode": self.query_graph_mode,
            "bindings": self.bindings,
            "binding_provenance": self.binding_provenance,
            "binding_candidates": self.binding_candidates,
            "edge_status": {key: value.value for key, value in self.edge_status.items()},
            "edge_support": self.edge_support,
            "deferred_trigger_sources": self.deferred_trigger_sources,
            "deferred": {k: v.model_dump(mode="json") for k, v in self.deferred.items()},
            "pending": {k: v.model_dump(mode="json") for k, v in self.pending.items()},
            "verified": {k: v.model_dump(mode="json") for k, v in self.verified.items()},
            "rejected": {k: v.model_dump(mode="json") for k, v in self.rejected.items()},
            "conflicts": self.conflicts,
            "status": self.status.value,
            "reason_codes": self.reason_codes,
            "operator_trace": self.operator_trace,
        }

    def graph_projection(self, *, max_claims: int | None = None) -> dict[str, Any]:
        """Return the compact verified graph view intended for UPDATE prompts."""
        claims = list(self.verified.values())
        if max_claims is not None:
            claims = [] if max_claims <= 0 else claims[-max_claims:]
        return {
            "bindings": dict(self.bindings),
            "query_graph": {
                "graph_id": self.query_graph.graph_id,
                "answer_node_id": self.query_graph.answer_node_id,
                "active_edge_ids": [
                    edge.id
                    for edge in self.query_graph.edges
                    if self.edge_status.get(edge.id) == QueryEdgeStatus.ACTIVE
                ],
                "dormant_edge_ids": [
                    edge.id
                    for edge in self.query_graph.edges
                    if self.edge_status.get(edge.id) == QueryEdgeStatus.DORMANT
                ],
                "edges": [
                    self._edge_projection(edge)
                    for edge in self.query_graph.edges
                ],
            },
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "subject": claim.subject,
                    "relation": claim.relation,
                    "object": claim.object,
                    "qualifiers": claim.qualifiers,
                    "polarity": claim.polarity.value,
                    "modality": claim.modality.value,
                    "matched_pattern_id": claim.matched_pattern_id,
                }
                for claim in claims
            ],
        }

    def _edge_projection(self, edge: QueryEdge) -> dict[str, Any]:
        subject_symbol = self._symbol(edge.subject_node_id)
        object_symbol = self._symbol(edge.object_node_id)
        nodes = {node.id: node for node in self.query_graph.nodes}

        def candidates(symbol: str) -> list[Any]:
            return [item["value"] for item in self.binding_candidates.get(symbol, [])]

        return {
            "id": edge.id,
            "subject": self.bindings.get(subject_symbol, subject_symbol),
            "subject_candidates": candidates(subject_symbol),
            "subject_type": nodes[edge.subject_node_id].value_type,
            "relation": edge.relation_key,
            "object": self.bindings.get(object_symbol, object_symbol),
            "object_candidates": candidates(object_symbol),
            "object_type": nodes[edge.object_node_id].value_type,
            "status": self.edge_status.get(edge.id, QueryEdgeStatus.DORMANT).value,
        }

    def _symbol(self, node_id: str) -> str:
        return next(node.symbol for node in self.query_graph.nodes if node.id == node_id)


@dataclass
class RuntimeResult:
    claim: Claim | None = None
    applied_action: Action | None = None
    pattern_id: str | None = None
    newly_bound: dict[str, Any] = field(default_factory=dict)
    deferred_matches: list[Claim] = field(default_factory=list)
    verification_candidates: list[Claim] = field(default_factory=list)
    events: list[RuntimeEvent] = field(default_factory=list)


class EvidenceRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        plan: QueryPlan,
        archive: RawArchive,
        store: SQLiteEventStore,
        verify_committed: bool = False,
        defer_unbound: bool = True,
        execute_operators: bool = True,
        query_graph_mode: str = "open",
        require_source_span: bool = False,
    ):
        self.run_id = run_id
        self.archive = archive
        self.store = store
        self.verify_committed = verify_committed
        self.defer_unbound = defer_unbound
        self.execute_operators = execute_operators
        if query_graph_mode not in {"open", "flat"}:
            raise ValueError("query_graph_mode must be 'open' or 'flat'")
        self.query_graph_mode = query_graph_mode
        self.require_source_span = require_source_span
        self._verification_matches: list[Claim] = []
        self._activated_edges: list[str] = []
        self._operators_executed = False
        self._source_window_index: dict[str, int] = {}
        query_graph = compile_open_query_graph(plan)
        self.state = RuntimeState(
            plan=plan, query_graph=query_graph, query_graph_mode=query_graph_mode
        )
        self._refresh_edge_states(emit=False)
        self._emit(
            "PLAN_CREATED",
            {
                "plan": plan.model_dump(mode="json"),
                "query_graph": query_graph.model_dump(mode="json"),
                "edge_status": {
                    key: value.value for key, value in self.state.edge_status.items()
                },
            },
        )
        self._emit(
            "OPEN_QUERY_GRAPH_CREATED",
            {
                "query_graph": query_graph.model_dump(mode="json"),
                "edge_status": {
                    key: value.value for key, value in self.state.edge_status.items()
                },
                "query_graph_mode": query_graph_mode,
            },
        )

    def register_read_window(self, window_index: int, source_refs: list[str]) -> None:
        """Record the actual read window for causal DEFER measurements."""
        for source_ref in source_refs:
            self._source_window_index[source_ref] = window_index

    def _edge_symbol(self, edge: QueryEdge, field: str) -> str:
        node_id = edge.subject_node_id if field == "subject" else edge.object_node_id
        return self.state._symbol(node_id)

    def _edge_pattern(self, edge: QueryEdge) -> Pattern:
        return Pattern(
            id=edge.id,
            subject=self._edge_symbol(edge, "subject"),
            relation=edge.relation,
            object=self._edge_symbol(edge, "object"),
            relation_family=edge.relation_family,
            cardinality=edge.cardinality,
            required=edge.required,
            qualifiers=edge.qualifiers,
        )

    def _edge_unbound_endpoints(self, edge: QueryEdge) -> int:
        values = (self._edge_symbol(edge, "subject"), self._edge_symbol(edge, "object"))
        return sum(_is_var(value) and value not in self.state.bindings for value in values)

    def _refresh_edge_states(self, *, emit: bool = True) -> None:
        """Refresh the executable query frontier after a binding transition."""
        for edge in self.state.query_graph.edges:
            prior = self.state.edge_status.get(edge.id)
            if prior in {QueryEdgeStatus.SATISFIED, QueryEdgeStatus.CONFLICTED}:
                continue
            status = QueryEdgeStatus.ACTIVE if self.query_graph_mode == "flat" else (
                QueryEdgeStatus.ACTIVE
                if self._edge_unbound_endpoints(edge) <= 1
                else QueryEdgeStatus.DORMANT
            )
            self.state.edge_status[edge.id] = status
            if emit and prior != status:
                self._emit(
                    "QUERY_EDGE_ACTIVATED" if status == QueryEdgeStatus.ACTIVE else "QUERY_EDGE_DORMANT",
                    {"edge_id": edge.id, "status": status.value},
                )
                if prior == QueryEdgeStatus.DORMANT and status == QueryEdgeStatus.ACTIVE:
                    self._activated_edges.append(edge.id)

    def _candidate_output(self, edge: QueryEdge | Pattern) -> str | None:
        value = edge.qualifiers.get("candidate_output")
        return value if isinstance(value, str) and _is_var(value) else None

    def _has_open_descendant(self, edge: QueryEdge | Pattern) -> bool:
        """Whether a path-consistent producer still has an unfinished suffix."""
        if edge.qualifiers.get("binding_policy") != "PATH_CONSISTENT":
            return False
        output = self._candidate_output(edge)
        if output is None:
            return False
        frontier = [output]
        visited: set[str] = set()
        while frontier:
            variable = frontier.pop(0)
            if variable in visited:
                continue
            visited.add(variable)
            for candidate_edge in self.state.query_graph.edges:
                if candidate_edge.id == edge.id:
                    continue
                pattern = self._edge_pattern(candidate_edge)
                if pattern.subject != variable:
                    continue
                if (
                    candidate_edge.required
                    and self.state.edge_status.get(candidate_edge.id)
                    not in {QueryEdgeStatus.SATISFIED, QueryEdgeStatus.CONFLICTED}
                ):
                    return True
                if _is_var(pattern.object):
                    frontier.append(pattern.object)
        return False

    def _edge_in_execution_frontier(self, edge: QueryEdge) -> bool:
        status = self.state.edge_status.get(edge.id)
        return status == QueryEdgeStatus.ACTIVE or (
            status == QueryEdgeStatus.SATISFIED and self._has_open_descendant(edge)
        )

    def drain_activated_edges(self) -> list[str]:
        edge_ids = list(dict.fromkeys(self._activated_edges))
        self._activated_edges = []
        return edge_ids

    def active_edge_ids(self) -> list[str]:
        return [
            edge.id
            for edge in self.state.query_graph.edges
            if self._edge_in_execution_frontier(edge)
        ]

    def edge_anchor_values(self, edge_id: str) -> list[str]:
        edge = next(edge for edge in self.state.query_graph.edges if edge.id == edge_id)
        pattern = self._edge_pattern(edge)
        values: list[str] = []
        for field, symbol in (("subject", pattern.subject), ("object", pattern.object)):
            value = self.state.bindings.get(symbol, symbol)
            if not _is_var(value):
                values.append(str(value))
                values.extend(self._pattern_aliases(pattern, field))
            if _is_var(symbol) and symbol != self._candidate_output(pattern):
                values.extend(
                    str(item["value"])
                    for item in self.state.binding_candidates.get(symbol, [])
                )
        return list(dict.fromkeys(value for value in values if value))

    def edge_projection(self, edge_id: str) -> dict[str, Any]:
        projection = self.state.graph_projection()
        edge = next(item for item in projection["query_graph"]["edges"] if item["id"] == edge_id)
        return edge

    def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        source_ref: str | None = None,
        stream_position: int | None = None,
        caused_by_event_seq: int | None = None,
        event_id: str | None = None,
    ) -> RuntimeEvent:
        raw = json.dumps(
            {
                "run_id": self.run_id,
                "event_type": event_type,
                "payload": payload,
                "source_ref": source_ref,
                "stream_position": stream_position,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        event = RuntimeEvent(
            event_id=event_id or hashlib.sha256(raw.encode()).hexdigest(),
            run_id=self.run_id,
            event_type=event_type,
            payload=payload,
            source_ref=source_ref,
            stream_position=stream_position,
            caused_by_event_seq=caused_by_event_seq,
            transaction_id=str(uuid4()),
        )
        return self.store.append_runtime_event(event)

    def _patterns_for(self, event: TripleEvent) -> list[Pattern]:
        active: list[Pattern] = []
        dormant: list[Pattern] = []
        for edge in self.state.query_graph.edges:
            if self.query_graph_mode != "flat" and not (
                self._edge_in_execution_frontier(edge)
                or self.state.edge_status.get(edge.id) == QueryEdgeStatus.DORMANT
            ):
                continue
            pattern = self._edge_pattern(edge)
            relation_key = event.matched_family or event.concrete_relation
            if (
                _norm(relation_key) == _norm(pattern.relation)
                or (pattern.relation_family and _norm(relation_key) == _norm(pattern.relation_family))
            ):
                (
                    active
                    if self.state.edge_status.get(edge.id) == QueryEdgeStatus.ACTIVE
                    else dormant
                ).append(pattern)
        candidates = [*active, *dormant]
        if event.pattern_hint:
            hinted = [p for p in candidates if p.id == event.pattern_hint]
            if hinted:
                # A model-provided hint is useful for disambiguation, but it
                # cannot suppress another same-relation pattern that is the
                # only one whose known endpoint actually matches the event.
                return [*hinted, *(p for p in candidates if p not in hinted)]
        return candidates

    @staticmethod
    def _pattern_aliases(pattern: Pattern, field: str) -> set[str]:
        """Return auditable aliases explicitly attached to a pattern endpoint."""
        aliases: set[str] = set()
        question_anchor = pattern.qualifiers.get(f"{field}_question_anchor")
        if isinstance(question_anchor, str) and question_anchor.strip():
            aliases.add(_norm(question_anchor))
        declared = pattern.qualifiers.get(f"{field}_aliases", [])
        if isinstance(declared, list):
            aliases.update(_norm(value) for value in declared if isinstance(value, str) and value.strip())
        return aliases

    def _endpoint_matches(
        self,
        pattern: Pattern,
        field: str,
        expected: Any,
        observed: Any,
        source_entry: Any | None = None,
    ) -> bool:
        if _is_var(expected):
            return True
        observed_key = _norm(observed)
        if observed_key == _norm(expected) or observed_key in self._pattern_aliases(pattern, field):
            return True
        if source_entry is None:
            return False
        title_key = _norm(source_entry.title)
        # A document title may resolve an omitted *expected* endpoint (for
        # example, "He was born in 1948" in the Martin Lee article).  It must
        # never resolve the observed endpoint instead: that would make an
        # inverse event such as (Martin Lee, director, Film A) satisfy the
        # directed query edge (Film A, director, ?director).
        expected_is_omitted = _norm(expected) not in _norm(source_entry.text)
        return (
            _norm(expected) == title_key
            and expected_is_omitted
            and _norm(observed) in _norm(source_entry.text)
        )

    def _pattern_matches_known(
        self, pattern: Pattern, event: TripleEvent, source_entry: Any | None = None
    ) -> bool:
        if not self._pattern_endpoint_matches(
            pattern, "subject", pattern.subject, event.subject, source_entry
        ):
            return False
        if not self._pattern_endpoint_matches(
            pattern, "object", pattern.object, event.object, source_entry
        ):
            return False
        return True

    def _pattern_endpoint_matches(
        self,
        pattern: Pattern,
        field: str,
        symbol: Any,
        observed: Any,
        source_entry: Any | None,
    ) -> bool:
        if _is_var(symbol):
            candidates = self.state.binding_candidates.get(symbol, [])
            if any(_norm(item["value"]) == _norm(observed) for item in candidates):
                return True
            if symbol == self._candidate_output(pattern) and self._has_open_descendant(pattern):
                return True
        expected = self.state.bindings.get(symbol, symbol)
        return self._endpoint_matches(pattern, field, expected, observed, source_entry)

    @staticmethod
    def _source_mentions(entry: Any, value: Any) -> bool:
        normalized_value = _norm(value)
        if not normalized_value:
            return False
        return normalized_value in _norm(entry.text) or normalized_value in _norm(entry.title)

    def _event_is_source_local(
        self,
        event: TripleEvent,
        source_entry: Any,
        pattern: Pattern,
    ) -> bool:
        """Cheap deterministic guard before expensive model verification.

        A sentence need not repeat its document title (pronouns are common).
        ACTIVE edges therefore require their already-grounded anchor in the
        sentence or title. DORMANT edges have no such anchor, so both concrete
        event endpoints must occur locally. This still permits normalized
        values such as British -> United Kingdom on an anchored edge.
        """
        if not event.span_hint:
            return not self.require_source_span
        if not contains_normalized_span(source_entry.text, event.span_hint):
            return False
        endpoints = (
            ("subject", pattern.subject, event.subject),
            ("object", pattern.object, event.object),
        )
        grounded: list[tuple[str, Any, Any]] = []
        for field, symbol, observed in endpoints:
            expected = self.state.bindings.get(symbol, symbol)
            if not _is_var(expected):
                grounded.append((field, expected, observed))
        if grounded:
            for field, expected, observed in grounded:
                aliases = self._pattern_aliases(pattern, field)
                disambiguators = {
                    alias
                    for alias in aliases
                    if _norm(expected) in alias and alias != _norm(expected)
                }
                if disambiguators:
                    if any(
                        self._source_mentions(source_entry, alias)
                        for alias in disambiguators
                    ):
                        return True
                    continue
                if (
                    self._source_mentions(source_entry, observed)
                    or self._source_mentions(source_entry, expected)
                    or any(
                        self._source_mentions(source_entry, alias) for alias in aliases
                    )
                ):
                    return True
            return False
        return any(
            self._source_mentions(source_entry, value)
            for value in (event.subject, event.object)
        )

    def _pattern_is_groundable(self, pattern: Pattern) -> bool:
        """A pattern is grounded if its non-variable side is known.

        This is the key delayed-binding distinction: ``(?person, birth_year,
        ?year)`` is related to the plan but is not yet assigned to a target
        person, so it must be deferred.
        """
        known_subject = not _is_var(pattern.subject) or pattern.subject in self.state.bindings
        known_object = not _is_var(pattern.object) or pattern.object in self.state.bindings
        return known_subject or known_object

    def _claim_from_event(self, event: TripleEvent, pattern: Pattern | None = None) -> Claim:
        claim_id = "claim_" + hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
        return Claim(
            claim_id=claim_id,
            subject=event.subject,
            relation=event.matched_family or event.concrete_relation,
            object=event.object,
            qualifiers=event.qualifiers,
            polarity=event.polarity,
            modality=event.modality,
            evidence_kind=EvidenceKind.RAW,
            disposition=Disposition.DEFERRED,
            evidence_assertions=[EvidenceAssertion(source_ref=event.source_ref)],
            operator_metadata={
                "observed_window_index": self._source_window_index.get(event.source_ref)
            },
            matched_pattern_id=pattern.id if pattern is not None else None,
        )

    def _register_binding_candidate(
        self,
        variable: str,
        value: Any,
        *,
        claim: Claim,
        source_ref: str,
    ) -> bool:
        candidates = self.state.binding_candidates.setdefault(variable, [])
        if any(_norm(item["value"]) == _norm(value) for item in candidates):
            return False
        candidate = {
            "value": value,
            "claim_id": claim.claim_id,
            "edge_id": claim.matched_pattern_id,
            "source_ref": source_ref,
            "stream_position": self.archive.entry(source_ref).stream_position,
            "window_index": self._source_window_index.get(source_ref),
        }
        candidates.append(candidate)
        self._emit(
            "QUERY_VARIABLE_CANDIDATE_REGISTERED",
            {"variable": variable, **candidate},
            source_ref=source_ref,
            stream_position=candidate["stream_position"],
        )
        # A new upstream candidate gives every unfinished consumer another
        # exact callback anchor without changing the selected path yet.
        for edge in self.state.query_graph.edges:
            pattern = self._edge_pattern(edge)
            if (
                pattern.subject == variable
                and self.state.edge_status.get(edge.id) == QueryEdgeStatus.ACTIVE
            ):
                self._activated_edges.append(edge.id)
        return True

    def _bind_values(
        self,
        pattern: Pattern,
        subject: Any,
        obj: Any,
        *,
        claim: Claim,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Register candidates and select only a source-consistent path.

        Producer edges with ``PATH_CONSISTENT`` retain alternatives. A
        consumer claim can later select one of those candidates by explicitly
        using it as its grounded endpoint.
        """
        selected: dict[str, Any] = {}
        previous: dict[str, Any] = {}
        discovered: dict[str, Any] = {}
        source_ref = claim.evidence_assertions[0].source_ref
        candidate_output = self._candidate_output(pattern)
        for slot, actual in ((pattern.subject, subject), (pattern.object, obj)):
            if not _is_var(slot):
                continue
            if self._register_binding_candidate(
                slot, actual, claim=claim, source_ref=source_ref
            ):
                discovered[slot] = actual
            current = self.state.bindings.get(slot)
            if current is None:
                self.state.bindings[slot] = actual
                selected[slot] = actual
                continue
            if _norm(current) == _norm(actual):
                continue
            # Do not use arrival order to choose between outputs of an
            # ambiguous producer. A verified downstream consumer selects the
            # candidate whose concrete endpoint it actually supports.
            if slot != candidate_output and any(
                _norm(item["value"]) == _norm(actual)
                for item in self.state.binding_candidates.get(slot, [])
            ):
                previous[slot] = current
                self.state.bindings[slot] = actual
                selected[slot] = actual
        return selected, previous, discovered

    def _bind_from(
        self, pattern: Pattern, event: TripleEvent, claim: Claim
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return self._bind_values(
            pattern, event.subject, event.object, claim=claim
        )

    def _bind_from_claim(
        self, claim: Claim
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Bind variables through the query edge the promoted Claim satisfied."""
        source_entry = self.archive.entry(claim.evidence_assertions[0].source_ref)
        for edge in self.state.query_graph.edges:
            if claim.matched_pattern_id and edge.id != claim.matched_pattern_id:
                continue
            pattern = self._edge_pattern(edge)
            if _norm(pattern.relation_key) != _norm(claim.relation):
                continue
            if not self._pattern_endpoint_matches(
                pattern, "subject", pattern.subject, claim.subject, source_entry
            ):
                continue
            if not self._pattern_endpoint_matches(
                pattern, "object", pattern.object, claim.object, source_entry
            ):
                continue
            return self._bind_values(
                pattern, claim.subject, claim.object, claim=claim
            )
        return {}, {}, {}

    def _record_bindings(
        self,
        new_bindings: dict[str, Any],
        *,
        previous_bindings: dict[str, Any] | None = None,
        source_ref: str,
        caused_by_event_seq: int | None,
    ) -> None:
        if not new_bindings:
            return
        source_position = self.archive.entry(source_ref).stream_position
        previous_bindings = previous_bindings or {}
        for variable, value in new_bindings.items():
            self.state.binding_provenance[variable] = {
                "source_ref": source_ref,
                "stream_position": source_position,
                "window_index": self._source_window_index.get(source_ref),
            }
            self._emit(
                "ENTITY_BOUND",
                {
                    "variable": variable,
                    "value": value,
                    "previous_value": previous_bindings.get(variable),
                },
                caused_by_event_seq=caused_by_event_seq,
            )
            if variable in previous_bindings:
                self._emit(
                    "QUERY_VARIABLE_REBOUND",
                    {
                        "variable": variable,
                        "previous_value": previous_bindings[variable],
                        "value": value,
                        "source_ref": source_ref,
                    },
                    source_ref=source_ref,
                    stream_position=source_position,
                    caused_by_event_seq=caused_by_event_seq,
                )
            self._emit(
                "QUERY_VARIABLE_BOUND",
                {
                    "variable": variable,
                    "value": value,
                    "source_ref": source_ref,
                    "stream_position": source_position,
                    "window_index": self._source_window_index.get(source_ref),
                },
                source_ref=source_ref,
                stream_position=source_position,
                caused_by_event_seq=caused_by_event_seq,
            )
        self._refresh_edge_states()

    def _mark_claim_verified(self, claim: Claim) -> None:
        claim.evidence_assertions = [
            assertion.model_copy(update={"verified": True})
            for assertion in claim.evidence_assertions
        ]

    def _mark_edge_satisfied(self, claim: Claim, *, caused_by_event_seq: int | None) -> None:
        edge_id = claim.matched_pattern_id
        if edge_id is None:
            return
        if edge_id not in self.state.edge_status:
            return
        supports = self.state.edge_support.setdefault(edge_id, [])
        if claim.claim_id not in supports:
            supports.append(claim.claim_id)
        prior = self.state.edge_status.get(edge_id)
        self.state.edge_status[edge_id] = QueryEdgeStatus.SATISFIED
        if prior != QueryEdgeStatus.SATISFIED:
            self._emit(
                "QUERY_EDGE_SATISFIED",
                {
                    "edge_id": edge_id,
                    "claim_id": claim.claim_id,
                    "source_refs": [item.source_ref for item in claim.evidence_assertions],
                },
                caused_by_event_seq=caused_by_event_seq,
            )

    def apply_event(self, event: TripleEvent) -> RuntimeResult:
        source_entry = self.archive.entry(event.source_ref)
        if source_entry.context_only:
            self._emit(
                "SOURCE_ACCESS_REJECTED",
                {"reason": "CONTEXT_ONLY_SOURCE", "event": event.model_dump(mode="json")},
                source_ref=event.source_ref,
                stream_position=source_entry.stream_position,
            )
            raise ValueError(f"source_ref {event.source_ref!r} is context_only")
        if event.source_order != source_entry.stream_position:
            self._emit(
                "EVENT_SOURCE_ORDER_MISMATCH",
                {
                    "model_source_order": event.source_order,
                    "manifest_source_order": source_entry.stream_position,
                },
                source_ref=event.source_ref,
                stream_position=source_entry.stream_position,
            )
            event = event.model_copy(update={"source_order": source_entry.stream_position})

        canonical_id = _canonical_event_id(event)
        if event.event_id != canonical_id:
            self._emit(
                "MODEL_EVENT_ID_NORMALIZED",
                {"model_event_id": event.event_id, "canonical_event_id": canonical_id},
                source_ref=event.source_ref,
                stream_position=source_entry.stream_position,
            )
            event = event.model_copy(update={"event_id": canonical_id})

        if not self.archive.contains(event.source_ref):
            self._emit(
                "SOURCE_ACCESS_REJECTED",
                {"reason": "FUTURE_SOURCE", "event": event.model_dump(mode="json")},
                source_ref=event.source_ref,
                stream_position=event.source_order,
            )
            raise FutureSourceAccessError(
                f"source_ref {event.source_ref!r} is not in the read prefix for run {self.run_id}"
            )

        previous = self.store.find_runtime_event(self.run_id, f"proposal:{event.event_id}")
        if previous is not None:
            claim_id = "claim_" + hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
            claim = (
                self.state.deferred.get(claim_id)
                or self.state.verified.get(claim_id)
                or self.state.rejected.get(claim_id)
            )
            return RuntimeResult(
                claim=claim,
                applied_action=self._action_for_claim(claim),
                events=self.store.list_runtime_events(self.run_id),
            )

        self._emit(
            "TRIPLE_EVENT_PROPOSED",
            {"event": event.model_dump(mode="json")},
            source_ref=event.source_ref,
            stream_position=event.source_order,
            event_id=f"proposal:{event.event_id}",
        )
        patterns = [
            p
            for p in self._patterns_for(event)
            if self._pattern_matches_known(p, event, source_entry)
        ]
        pattern = patterns[0] if patterns else None
        if pattern is None:
            claim = self._claim_from_event(event, pattern)
            claim.disposition = Disposition.SKIPPED
            self._emit("CLAIM_SKIPPED", {"claim": claim.model_dump(mode="json")}, source_ref=event.source_ref)
            return RuntimeResult(claim=claim, applied_action=Action.SKIP, events=self.store.list_runtime_events(self.run_id))
        if not self._event_is_source_local(event, source_entry, pattern):
            claim = self._claim_from_event(event, pattern)
            claim.disposition = Disposition.SKIPPED
            self._emit(
                "SOURCE_LOCAL_CANDIDATE_REJECTED",
                {
                    "claim": claim.model_dump(mode="json"),
                    "reason": "ENDPOINT_OR_SPAN_NOT_IN_SOURCE",
                },
                source_ref=event.source_ref,
                stream_position=source_entry.stream_position,
            )
            return RuntimeResult(
                claim=claim,
                applied_action=Action.SKIP,
                events=self.store.list_runtime_events(self.run_id),
            )
        claim = self._claim_from_event(event, pattern)

        if self._pattern_is_groundable(pattern):
            claim.disposition = Disposition.COMMITTED
            applied = Action.COMMIT
            self._emit(
                "CLAIM_COMMITTED",
                {
                    "claim": claim.model_dump(mode="json"),
                    "pattern_id": pattern.id,
                    "verification_required": self.verify_committed,
                },
                source_ref=event.source_ref,
            )
            if self.verify_committed:
                self.state.pending[claim.claim_id] = claim
                return RuntimeResult(
                    claim=claim,
                    applied_action=applied,
                    pattern_id=pattern.id,
                    verification_candidates=[claim],
                    events=self.store.list_runtime_events(self.run_id),
                )
            newly_bound, previous_bindings, discovered = self._bind_from(
                pattern, event, claim
            )
            self._mark_claim_verified(claim)
            self.state.verified[claim.claim_id] = claim
            self._mark_edge_satisfied(claim, caused_by_event_seq=None)
            self._record_bindings(
                newly_bound,
                previous_bindings=previous_bindings,
                source_ref=event.source_ref,
                caused_by_event_seq=None,
            )
            matches = self._lookup_deferred({**newly_bound, **discovered})
            return RuntimeResult(
                claim=claim,
                applied_action=applied,
                pattern_id=pattern.id,
                newly_bound=newly_bound,
                deferred_matches=matches,
                events=self.store.list_runtime_events(self.run_id),
            )

        claim.disposition = Disposition.DEFERRED
        if not self.defer_unbound:
            claim.disposition = Disposition.SKIPPED
            self._emit(
                "CLAIM_SKIPPED",
                {
                    "claim": claim.model_dump(mode="json"),
                    "pattern_id": pattern.id,
                    "reason": "DEFER_DISABLED",
                },
                source_ref=event.source_ref,
            )
            return RuntimeResult(
                claim=claim,
                applied_action=Action.SKIP,
                pattern_id=pattern.id,
                events=self.store.list_runtime_events(self.run_id),
            )
        applied = Action.DEFER
        self.state.deferred[claim.claim_id] = claim
        self._emit("CLAIM_DEFERRED", {"claim": claim.model_dump(mode="json"), "pattern_id": pattern.id}, source_ref=event.source_ref)
        return RuntimeResult(
            claim=claim,
            applied_action=applied,
            pattern_id=pattern.id,
            events=self.store.list_runtime_events(self.run_id),
        )

    def apply_events(self, events: list[TripleEvent]) -> list[RuntimeResult]:
        """Apply a window's events in manifest/source order.

        The model is instructed to emit this order, but Runtime reorders using
        the source positions supplied by the manifest-derived event metadata.
        """
        readable: list[TripleEvent] = []
        rejected: list[RuntimeResult] = []
        for event in events:
            try:
                self.archive.entry(event.source_ref)
            except FutureSourceAccessError:
                self._emit(
                    "SOURCE_ACCESS_REJECTED",
                    {"reason": "FUTURE_SOURCE", "event": event.model_dump(mode="json")},
                    source_ref=event.source_ref,
                    stream_position=event.source_order,
                )
                rejected.append(
                    RuntimeResult(claim=None, applied_action=Action.SKIP, events=self.store.list_runtime_events(self.run_id))
                )
                continue
            readable.append(event)
        ordered = sorted(
            readable,
            key=lambda item: (
                self.archive.entry(item.source_ref).stream_position,
                item.span_hint or "",
                item.event_id,
            ),
        )
        if [event.event_id for event in ordered] != [event.event_id for event in events]:
            self._emit(
                "EVENTS_REORDERED",
                {
                    "model_order": [event.event_id for event in events],
                    "applied_order": [event.event_id for event in ordered],
                },
            )
        return [*rejected, *(self.apply_event(event) for event in ordered)]

    @staticmethod
    def _action_for_claim(claim: Claim | None) -> Action | None:
        if claim is None:
            return None
        return {
            Disposition.DEFERRED: Action.DEFER,
            Disposition.COMMITTED: Action.COMMIT,
            Disposition.PROMOTED: Action.COMMIT,
            Disposition.SKIPPED: Action.SKIP,
            Disposition.REJECTED: Action.SKIP,
        }[claim.disposition]

    def _lookup_deferred(self, new_bindings: dict[str, Any]) -> list[Claim]:
        if not new_bindings:
            return []
        grounded_requirements: set[tuple[str, str, str]] = set()
        for edge in self.state.query_graph.edges:
            pattern = self._edge_pattern(edge)
            relation = _norm(pattern.relation_key)
            if _is_var(pattern.subject) and pattern.subject in new_bindings:
                grounded_requirements.add(("subject", _norm(new_bindings[pattern.subject]), relation))
            if _is_var(pattern.object) and pattern.object in new_bindings:
                grounded_requirements.add(("object", _norm(new_bindings[pattern.object]), relation))
        matches: list[Claim] = []
        for claim in self.state.deferred.values():
            claim_entry = self.archive.entry(claim.evidence_assertions[0].source_ref)
            for side, expected, relation in grounded_requirements:
                observed = claim.subject if side == "subject" else claim.object
                if _norm(claim.relation) != relation:
                    continue
                if _norm(observed) == expected or (
                    _norm(claim_entry.title) == expected
                    and self._source_mentions(claim_entry, observed)
                ):
                    matches.append(claim)
                    break
        binding_records = [
            self.state.binding_provenance[variable]
            for variable in new_bindings
            if variable in self.state.binding_provenance
        ]
        binding_source_refs = sorted({str(item["source_ref"]) for item in binding_records})
        for claim in matches:
            # The trigger is retained so a same-sentence promotion can be
            # distinguished from genuinely early evidence in evaluation.
            if binding_source_refs:
                trigger = min(
                    binding_records,
                    key=lambda item: (
                        item.get("window_index") if item.get("window_index") is not None else 10**9,
                        item["stream_position"],
                    ),
                )
                self.state.deferred_trigger_sources[claim.claim_id] = dict(trigger)
        self._emit(
            "DEFERRED_LOOKUP",
            {
                "claim_ids": [claim.claim_id for claim in matches],
                "bindings": new_bindings,
                "binding_source_refs": binding_source_refs,
                "hit": bool(matches),
                "requirement_count": len(grounded_requirements),
            },
        )
        return matches

    def apply_verification(self, result: VerifyResult) -> Claim | None:
        origin = "DEFERRED" if result.claim_id in self.state.deferred else (
            "PENDING" if result.claim_id in self.state.pending else "VERIFIED"
        )
        claim = (
            self.state.deferred.get(result.claim_id)
            or self.state.pending.get(result.claim_id)
            or self.state.verified.get(result.claim_id)
        )
        if claim is None:
            raise KeyError(result.claim_id)
        claim.verification = result.status
        if result.normalized_claim is not None:
            claim = result.normalized_claim
        if result.status == VerifyStatus.ACCEPT:
            existing_refs = {assertion.source_ref for assertion in claim.evidence_assertions}
            for source_ref in result.expanded_source_refs:
                if source_ref not in existing_refs:
                    claim.evidence_assertions.append(
                        EvidenceAssertion(
                            source_ref=source_ref,
                            raw_text=result.supporting_text,
                        )
                    )
                    existing_refs.add(source_ref)
            if result.supporting_text:
                claim.evidence_assertions = [
                    assertion.model_copy(
                        update={
                            "raw_text": result.supporting_text
                            if assertion.source_ref in result.expanded_source_refs
                            else assertion.raw_text
                        }
                    )
                    for assertion in claim.evidence_assertions
                ]
            claim.disposition = Disposition.PROMOTED
            self.state.deferred.pop(result.claim_id, None)
            self.state.pending.pop(result.claim_id, None)
            self._mark_claim_verified(claim)
            self.state.verified[claim.claim_id] = claim
            promoted_event = self._emit(
                "CLAIM_PROMOTED",
                {"claim": claim.model_dump(mode="json"), "origin": origin},
            )
            self._mark_edge_satisfied(claim, caused_by_event_seq=promoted_event.event_seq)
            new_bindings, previous_bindings, discovered = self._bind_from_claim(claim)
            claim_source_ref = claim.evidence_assertions[0].source_ref
            self._record_bindings(
                new_bindings,
                previous_bindings=previous_bindings,
                source_ref=claim_source_ref,
                caused_by_event_seq=promoted_event.event_seq,
            )
            trigger = self.state.deferred_trigger_sources.pop(result.claim_id, None)
            if origin == "DEFERRED" and trigger:
                trigger_source_ref = str(trigger["source_ref"])
                claim_position = self.archive.entry(claim_source_ref).stream_position
                trigger_position = self.archive.entry(trigger_source_ref).stream_position
                claim_window = claim.operator_metadata.get("observed_window_index")
                trigger_window = trigger.get("window_index")
                is_cross_window = (
                    isinstance(claim_window, int)
                    and isinstance(trigger_window, int)
                    and claim_window < trigger_window
                )
                event_type = (
                    "CROSS_WINDOW_DEFERRED_PROMOTED"
                    if is_cross_window
                    else "NON_EARLY_DEFERRED_PROMOTED"
                )
                self._emit(
                    event_type,
                    {
                        "claim_id": claim.claim_id,
                        "deferred_source_ref": claim_source_ref,
                        "binding_source_ref": trigger_source_ref,
                        "deferred_stream_position": claim_position,
                        "binding_stream_position": trigger_position,
                        "deferred_window_index": claim_window,
                        "binding_window_index": trigger_window,
                        "window_distance": (
                            trigger_window - claim_window if is_cross_window else 0
                        ),
                    },
                    caused_by_event_seq=promoted_event.event_seq,
                )
            self._verification_matches.extend(
                self._lookup_deferred({**new_bindings, **discovered})
            )
        elif result.status == VerifyStatus.REJECT:
            claim.disposition = Disposition.REJECTED
            self.state.deferred.pop(result.claim_id, None)
            self.state.pending.pop(result.claim_id, None)
            self.state.rejected[claim.claim_id] = claim
            self._emit("VERIFY_REJECTED", {"claim": claim.model_dump(mode="json"), "reason": result.reason})
        elif result.status == VerifyStatus.CONFLICT:
            claim.disposition = Disposition.REJECTED
            self.state.conflicts.setdefault(claim.claim_id, []).append(result.reason or "VERIFY_CONFLICT")
            if claim.matched_pattern_id in self.state.edge_status:
                self.state.edge_status[claim.matched_pattern_id] = QueryEdgeStatus.CONFLICTED
                self._emit(
                    "QUERY_EDGE_CONFLICTED",
                    {
                        "edge_id": claim.matched_pattern_id,
                        "claim_id": claim.claim_id,
                        "reason": result.reason,
                    },
                )
            self._emit("VERIFY_CONFLICT", {"claim": claim.model_dump(mode="json"), "reason": result.reason})
        else:
            self._emit(
                "VERIFY_NEED_MORE_CONTEXT",
                {"claim": claim.model_dump(mode="json"), "reason": result.reason},
            )
        return claim

    def drain_verification_matches(self) -> list[Claim]:
        """Return deferred claims activated by the last accepted verification."""
        matches = self._verification_matches
        self._verification_matches = []
        return matches

    def evidence_pack(self) -> EvidencePack:
        target = self.state.plan.answer_contract.target if self.state.plan.answer_contract else None
        runtime_answer = self.state.bindings.get(target) if target else next(
            (
                item.get("value")
                for item in reversed(self.state.operator_trace)
                if item.get("status") == "EXECUTED" and item.get("value") is not None
            ),
            None,
        )
        claims = [
            claim
            for claim in self.state.verified.values()
            if self._claim_matches_selected_path(claim)
        ]
        selected_claim_ids = {claim.claim_id for claim in claims}
        return EvidencePack(
            claims=claims,
            operator_trace=list(self.state.operator_trace),
            answer_value=runtime_answer,
            answer_type=self.state.plan.answer_contract.type if self.state.plan.answer_contract else None,
            query_graph={
                "graph": self.state.query_graph.model_dump(mode="json"),
                "edge_status": {key: value.value for key, value in self.state.edge_status.items()},
            },
            support_mapping={
                key: [claim_id for claim_id in value if claim_id in selected_claim_ids]
                for key, value in self.state.edge_support.items()
            },
        )

    def _claim_matches_selected_path(self, claim: Claim) -> bool:
        """Remove verified alternatives that are outside the selected proof."""
        if claim.matched_pattern_id is None:
            return True
        edge = next(
            (edge for edge in self.state.query_graph.edges if edge.id == claim.matched_pattern_id),
            None,
        )
        if edge is None:
            return True
        pattern = self._edge_pattern(edge)
        for symbol, actual in (
            (pattern.subject, claim.subject),
            (pattern.object, claim.object),
        ):
            if (
                _is_var(symbol)
                and symbol in self.state.bindings
                and _norm(self.state.bindings[symbol]) != _norm(actual)
            ):
                return False
        return True

    def finalize(self) -> EvidencePack | None:
        """Run the EOS-only sufficiency gate and produce canonical evidence."""
        satisfied = {
            edge_id
            for edge_id, status in self.state.edge_status.items()
            if status == QueryEdgeStatus.SATISFIED
        }
        required = {edge.id for edge in self.state.query_graph.edges if edge.required}
        reasons: list[str] = []
        missing = sorted(required - satisfied)
        if missing:
            reasons.append("REQUIRED_PATTERN_MISSING:" + ",".join(missing))
        if self.state.conflicts:
            reasons.append("UNRESOLVED_CONFLICT")
        contract = self.state.plan.answer_contract
        if reasons:
            self.state.status = (
                RuntimeStatus.CONFLICTED
                if "UNRESOLVED_CONFLICT" in reasons
                else RuntimeStatus.INSUFFICIENT
            )
            self.state.reason_codes = reasons
            self._emit(
                "SUFFICIENCY_CHECKED",
                {"sufficient": False, "reason_codes": reasons},
            )
            self._emit(
                "RUN_STATUS",
                {"status": self.state.status.value, "reason_codes": reasons},
            )
            return None
        if self.execute_operators:
            try:
                self._execute_plan_operators()
            except OperatorError as exc:
                self.state.status = RuntimeStatus.UNSUPPORTED
                self.state.reason_codes = [f"OPERATOR_ERROR:{exc}"]
                self._emit(
                    "RUN_STATUS",
                    {"status": self.state.status.value, "reason_codes": self.state.reason_codes},
                )
                return None
        elif self.state.plan.operators:
            self._emit(
                "OPERATOR_EXECUTION_SKIPPED",
                {
                    "reason": "EVIDENCE_ANSWER_MODE",
                    "operator_ids": [operator.id for operator in self.state.plan.operators],
                },
            )
        if contract and contract.target and contract.target not in self.state.bindings:
            self.state.status = RuntimeStatus.INSUFFICIENT
            self.state.reason_codes = ["ANSWER_TARGET_UNBOUND:" + contract.target]
            self._emit(
                "RUN_STATUS",
                {"status": self.state.status.value, "reason_codes": self.state.reason_codes},
            )
            return None
        self.state.status = RuntimeStatus.ANSWERED
        self.state.reason_codes = []
        pack = self.evidence_pack()
        self._emit(
            "SUFFICIENCY_CHECKED",
            {"sufficient": True, "claim_ids": [claim.claim_id for claim in pack.claims]},
        )
        self._emit(
            "RUN_STATUS",
            {
                "status": self.state.status.value,
                "reason_codes": [],
                "answer_value": pack.answer_value,
            },
        )
        return pack

    def _execute_plan_operators(self) -> None:
        """Execute the small deterministic operator registry at EOS.

        Operators are declarative: inputs can reference bindings or literal
        lists, and an optional ``output``/``result`` parameter writes a derived
        value back into Runtime bindings for the answer contract.
        """
        if self._operators_executed:
            return
        for operator in self.state.plan.operators:
            target: str | None = None
            value: Any = None
            operator_type = operator.type.upper()
            if operator_type == "PROJECT":
                target = operator.params.get("target") or operator.params.get("output")
                source = operator.params.get("source") or (operator.inputs[0] if operator.inputs else None)
                if not target or not isinstance(source, str) or not _is_var(source):
                    raise OperatorError("PROJECT requires source and output variables")
                if source not in self.state.bindings:
                    raise OperatorError(f"PROJECT source is unbound: {source}")
                value = self.state.bindings[source]
            elif operator_type in {"DIRECT", "PATH_JOIN", "REGISTERED_RULE", "RULE", "FILTER"}:
                # These are structural declarations consumed by UPDATE; the
                # Runtime records them but does not invent derived facts.
                self.state.operator_trace.append(
                    {"operator_id": operator.id, "type": operator_type, "inputs": operator.inputs, "output": None, "value": None, "status": "DECLARED"}
                )
                self._emit("OPERATOR_EXECUTED", self.state.operator_trace[-1])
                continue
            else:
                args = [self._resolve_operator_value(value) for value in operator.inputs]
                if operator_type == "COUNT":
                    values = args[0] if len(args) == 1 and isinstance(args[0], list) else args
                    value = execute_operator(operator_type, values)
                elif operator_type in {"INTERSECTION", "UNION"}:
                    value = execute_operator(
                        operator_type,
                        args[0] if isinstance(args[0], list) else [args[0]],
                        args[1] if len(args) > 1 and isinstance(args[1], list) else args[1:],
                    )
                elif operator_type == "COMPARE":
                    if len(args) != 2:
                        raise OperatorError("COMPARE requires exactly two inputs")
                    value = execute_operator(
                        operator_type,
                        args[0],
                        args[1],
                        operator.params.get("comparison", operator.params.get("operator", "EQ")),
                    )
                elif operator_type in {"ARGMIN", "ARGMAX"}:
                    candidates = operator.params.get("candidates")
                    if not isinstance(candidates, list):
                        raise OperatorError(f"{operator_type} requires params.candidates")
                    resolved_candidates = [self._resolve_operator_value(item) for item in candidates]
                    value = execute_operator(operator_type, args, resolved_candidates)
                else:
                    value = execute_operator(operator_type, *args)
                target = operator.params.get("output") or operator.params.get("output_var") or operator.params.get("result")
            if target:
                if not isinstance(target, str) or not _is_var(target):
                    raise OperatorError(f"operator output must be a variable: {target!r}")
                self.state.bindings[target] = value
            trace = {
                "operator_id": operator.id,
                "type": operator_type,
                "inputs": operator.inputs,
                "output": target,
                "value": value,
                "status": "EXECUTED",
            }
            self.state.operator_trace.append(trace)
            executed_event = self._emit("OPERATOR_EXECUTED", trace)
            if target:
                self._emit(
                    "ENTITY_BOUND",
                    {"variable": target, "value": value, "binding_source": "operator"},
                    caused_by_event_seq=executed_event.event_seq,
                )
        self._operators_executed = True

    def _resolve_operator_value(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._resolve_operator_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve_operator_value(item) for key, item in value.items()}
        if isinstance(value, str) and _is_var(value):
            if value not in self.state.bindings:
                raise OperatorError(f"operator input is unbound: {value}")
            return self.state.bindings[value]
        return value
