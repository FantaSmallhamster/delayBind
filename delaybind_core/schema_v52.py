"""V5.2 contracts. These never deserialize historical graph records as v3."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Subquery(Strict):
    id: str = Field(pattern=r"^Q[0-9]+$")
    template: str = Field(min_length=1)
    output: str = Field(pattern=r"^\?[A-Za-z_][A-Za-z_0-9]*$")
    inputs: dict[str, str] = Field(default_factory=dict)
    cardinality: Literal["SINGLE", "SET"] = "SINGLE"
    requires_complete_set: bool = False

    @property
    def depends_on(self) -> list[str]:
        return sorted(set(self.inputs.values()))


class QueryPlanV3(Strict):
    schema_version: Literal["v3"] = "v3"
    plan_id: str
    queries: list[Subquery] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_dag(self) -> "QueryPlanV3":
        by_id = {q.id: q for q in self.queries}
        if len(by_id) != len(self.queries):
            raise ValueError("DUPLICATE_QUERY_ID")
        if len({q.output for q in self.queries}) != len(self.queries):
            raise ValueError("DUPLICATE_OUTPUT_PRODUCER")
        for q in self.queries:
            variables = set(re.findall(r"\?[A-Za-z_][A-Za-z_0-9]*", q.template))
            if variables != set(q.inputs) or q.output in variables:
                raise ValueError(f"INPUT_TEMPLATE_MISMATCH:{q.id}")
            if q.requires_complete_set and q.cardinality != "SET":
                raise ValueError(f"COMPLETE_SET_CARDINALITY:{q.id}")
            for var, parent in q.inputs.items():
                if parent not in by_id or by_id[parent].output != var:
                    raise ValueError(f"INVALID_INPUT_PRODUCER:{q.id}:{var}")
        visited, visiting = set(), set()

        def visit(qid: str) -> None:
            if qid in visiting:
                raise ValueError("CYCLIC_QUERY_PLAN")
            if qid in visited:
                return
            visiting.add(qid)
            for parent in by_id[qid].depends_on:
                visit(parent)
            visiting.remove(qid)
            visited.add(qid)

        for qid in by_id:
            visit(qid)
        return self


class QueryDependencyEdge(Strict):
    producer_query_id: str
    consumer_query_id: str
    variable: str
    binding_id: str | None = None
    binding_revision: int | None = None
    value: Any = None


class BindingRecord(Strict):
    binding_id: str
    producer_query_id: str
    variable: str
    value: Any
    value_kind: Literal["TEXT", "NUMBER", "BOOLEAN", "SET", "STRUCTURED"]
    kind: Literal["DIRECT", "INFERRED"]
    query_version: int
    input_signature: str
    binding_revision: int
    direct_fact_ids: list[str]
    parent_binding_ids: list[str]
    source_refs: list[str]
    created_from_context: str
    valid: bool = True


class RawEvidence(Strict):
    source_ref: str
    text: str
    kind: Literal["sentence", "fragment", "heading", "chunk"]
    complete: bool
    text_sha256: str


class FactNode(Strict):
    model_config = ConfigDict(extra="forbid", frozen=True)
    fact_id: str
    text: str
    source_refs: tuple[str, ...]
    observed_window: int
    supersedes_fact_id: str | None = None


class FactUse(Strict):
    query_id: str
    input_signature: str
    fact_id: str
    status: Literal["PENDING", "CANDIDATE", "ACCEPTED", "HELD", "REJECTED", "CONFLICT", "INVALIDATED"]
    review_context_id: str | None = None
    decision_revision: int = 0
    reviewed_source_refs: list[str] = Field(default_factory=list)
    reason_code: str | None = None
    evidence_context_signature: str | None = None


class QueryExecutionV52(Strict):
    status: Literal["DORMANT", "ACTIVE", "RESOLVED"] = "DORMANT"
    version: int = 1
    input_signature: str = "I0"
    current_binding_id: str | None = None
    review_barrier: bool = False


class RecallJob(Strict):
    job_id: str
    query_id: str
    input_signature: str
    query_version: int
    bucket_version: str
    reason: str
    trigger_binding_id: str | None = None
    candidate_ids: list[str]
    selected_fact_ids: list[str] = Field(default_factory=list)
    scan_offset: int = 0
    stage: Literal["SCAN", "REVIEW", "DONE", "INVALIDATED", "SCAN_INCOMPLETE"] = "SCAN"


class EvidencePackV52(Strict):
    navigation: dict[str, Any] = Field(default_factory=dict)
    raw_evidence: list[RawEvidence] = Field(default_factory=list)
    unresolved_or_conflicts: list[dict[str, Any]] = Field(default_factory=list)


class MemoryContext(Strict):
    context_id: str
    state_revision: int
    query_graph: dict[str, Any]
    working_memory: EvidencePackV52
    pending_uses: list[dict[str, str]]
    plan_hints: list[FactNode] = Field(default_factory=list)
    allowed_fact_ids: list[str]
    allowed_source_refs: list[str]
    bindable_query_ids: list[str]
    input_signatures: dict[str, str]
    review_barriers: dict[str, Any]
    scope_closed: bool
    context_request_limits: dict[str, int]


class SubqueryStateV52(Strict):
    protocol_version: Literal["v5.2"] = "v5.2"
    plan: QueryPlanV3
    executions: dict[str, QueryExecutionV52] = Field(default_factory=dict)
    binding_store: dict[str, BindingRecord] = Field(default_factory=dict)
    facts: dict[str, FactNode] = Field(default_factory=dict)
    uses: dict[str, FactUse] = Field(default_factory=dict)
    defer_workspace: dict[str, list[str]] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    recall_jobs: dict[str, RecallJob] = Field(default_factory=dict)
    review_queue: list[str] = Field(default_factory=list)
    extra_context_refs: dict[str, list[str]] = Field(default_factory=dict)
    context_requests: dict[str, dict[str, Any]] = Field(default_factory=dict)
    context_expansions: int = 0
    active_view_ids: list[str] | None = None
    query_edges: list[QueryDependencyEdge] = Field(default_factory=list)
    binding_ports: list[QueryDependencyEdge] = Field(default_factory=list)
    navigation_links: list[dict[str, Any]] = Field(default_factory=list)
    state_revision: int = 0
    scope_closed: bool = False
    cursor_state: dict[str, Any] = Field(default_factory=dict)
    pending_window: list[str] = Field(default_factory=list)
    read_watermark: int = -1
    status: str = "RUNNING"
    reason_codes: list[str] = Field(default_factory=list)
    answer: dict[str, Any] | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def bindings(self) -> dict[str, Any]:
        return {b.variable: b.value for b in self.binding_store.values() if b.valid}

    def export(self) -> dict[str, Any]:
        return {**self.model_dump(mode="json"), "bindings": self.bindings}


def use_key(query_id: str, signature: str, fact_id: str) -> str:
    return f"{query_id}|{signature}|{fact_id}"
