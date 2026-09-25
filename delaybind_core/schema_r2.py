"""Versioned R2 state and internal contracts; model wire format remains text."""

from typing import Any, Literal
from pydantic import ConfigDict, Field, model_validator

from .schema_v52 import Strict, QueryPlanV3, FactNode, RawEvidence, EvidencePackV52, digest

PROTOCOL = "v5.2-r2"
CONTRACT = "memory-v5.2-bind-rebind-1"


class EvidenceQueryR2(Strict):
    """A demand template, not a prediction of the number of answers."""
    id: str = Field(pattern=r"^Q[0-9]+$")
    template: str = Field(min_length=1)
    output: str = Field(pattern=r"^\?[A-Za-z_][A-Za-z_0-9]*$")
    inputs: dict[str, str] = Field(default_factory=dict)
    requires_complete_set: bool = False
    allow_upstream_only: bool = False

    @property
    def depends_on(self):
        return sorted(set(self.inputs.values()))


class EvidencePlanR2(Strict):
    schema_version: Literal["r2-members-1"] = "r2-members-1"
    plan_id: str
    queries: list[EvidenceQueryR2] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_dag(self):
        # Reuse only the existing DAG/variable validator. Cardinality is absent
        # from this schema and from the persisted/model-visible R2 plan.
        QueryPlanV3(plan_id=self.plan_id, queries=[
            {**q.model_dump(exclude={"allow_upstream_only"}), "cardinality": "SET"} for q in self.queries])
        for q in self.queries:
            if q.allow_upstream_only and (not q.inputs or not any(q.id in child.depends_on for child in self.queries)):
                raise ValueError("UPSTREAM_ONLY_REQUIRES_INTERMEDIATE_DEPENDENCY:" + q.id)
        return self


def member_plan(plan):
    if isinstance(plan, EvidencePlanR2):
        return plan
    return EvidencePlanR2(plan_id=plan.plan_id, queries=[
        q.model_dump(exclude={"cardinality"}) for q in plan.queries])


class MemberResult(Strict):
    value: Any
    support_fact_ids: list[str] = Field(default_factory=list)
    decision_source_refs: list[str] = Field(default_factory=list)


class BindingMemberR2(Strict):
    member_id: str
    branch_id: str
    value: Any
    direct_fact_ids: list[str]
    parent_member_ids: list[str]
    # Assignments to ancestor query nodes: a natural join cannot combine
    # siblings that descend from different members of the same ancestor.
    lineage: dict[str, str] = Field(default_factory=dict)
    decision_source_refs: list[str] = Field(default_factory=list)


class MemberBranchR2(Strict):
    branch_id: str
    bound_inputs: dict[str, Any] = Field(default_factory=dict)
    parent_member_ids: list[str] = Field(default_factory=list)
    lineage: dict[str, str] = Field(default_factory=dict)


class EvidenceReview(Strict):
    model_config = ConfigDict(extra="forbid", strict=True)
    fact_id: str
    verdict: Literal["ACCEPT", "REJECT", "HOLD", "CONFLICT"]
    checked_refs: list[str] = Field(min_length=1)
    reason_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")
    reason: str = Field(min_length=1, max_length=480)
    correction: "Correction | None" = None
    context_request: "ContextRequest | None" = None

    @model_validator(mode="after")
    def compatible(self):
        if self.correction is not None and self.verdict != "ACCEPT":
            raise ValueError("CORRECTION_REQUIRES_ACCEPT")
        if self.context_request is not None and self.verdict != "HOLD":
            raise ValueError("CONTEXT_REQUIRES_HOLD")
        return self


class Correction(Strict):
    local_id: str = Field(pattern=r"^N[0-9]+$")
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


class ContextRequest(Strict):
    anchor: str
    before: int = Field(ge=0)
    after: int = Field(ge=0)


class BindingResult(Strict):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    state: Literal["BOUND", "UNBOUND", "NOOP"]
    value: Any = None
    kind: Literal["DIRECT", "INFERRED"] = "DIRECT"
    support_fact_ids: list[str] = Field(default_factory=list)
    decision_source_refs: list[str] = Field(default_factory=list)
    reason_code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")
    reason: str = Field(min_length=1, max_length=480)
    members: list[MemberResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_result(self):
        import json
        json.dumps(self.value, allow_nan=False)
        if self.state in {"UNBOUND", "NOOP"}:
            if self.value is not None or self.support_fact_ids or self.members:
                raise ValueError(f"{self.state}_HAS_VALUE_OR_SUPPORT")
        elif self.value is None or (isinstance(self.value, str) and (
                not self.value.strip() or self.value.strip().upper() in {"NONE", "UNKNOWN", "NULL"}
                or self.value.strip().startswith("?"))):
            raise ValueError("BOUND_VALUE_REQUIRED")
        return self


class MemoryAction(Strict):
    op: Literal["BIND", "REBIND"]
    query_id: str
    evidence_reviews: list[EvidenceReview]
    binding_result: BindingResult | None


class MemoryResponseR2(Strict):
    schema_version: Literal["memory-v5.2-bind-rebind-1"] = CONTRACT
    context_id: str
    review_id: str
    operations: list[MemoryAction] = Field(min_length=1, max_length=1)
    ignored_lines: list[dict[str, str]] = Field(default_factory=list)
    format_normalizations: list[dict[str, str]] = Field(default_factory=list)


class Route(Strict):
    query_id: str
    observed_status: Literal["ACTIVE", "DORMANT", "RESOLVED"]


class Observation(Strict):
    text: str = Field(min_length=1)
    # Fact-only mode intentionally stores no model-selected raw anchors.
    source_refs: list[str] = Field(default_factory=list)
    routes: list[Route] = Field(min_length=1)


class Hint(Strict):
    text: str = Field(min_length=1)
    # Like observations, hints are deliberately source-free in fact-only mode.
    source_refs: list[str] = Field(default_factory=list)


class UpdateResponseR2(Strict):
    schema_version: Literal["update-v5.2-r2"] = "update-v5.2-r2"
    context_id: str
    facts: list[Observation] = Field(default_factory=list)
    hints: list[Hint] = Field(default_factory=list)


class PendingChunkR2(Strict):
    chunk_index: int = Field(ge=0)
    token_start: int = Field(ge=0)
    token_end: int = Field(ge=0)
    chunk_text: str


class PlanRepairFailureR2(Strict):
    status: Literal["REJECTED"] = "REJECTED"
    hint_ids: list[str]
    base_plan_hash: str
    basis_hash: str
    context_id: str
    base_state_revision: int
    attempt_count: int
    error_codes: list[str]
    response_hashes: list[str]
    policy_version: Literal["optional-repair-reject-v1"] = "optional-repair-reject-v1"


class UpdateRepairProgressR2(Strict):
    context_id: str
    next_attempt: int
    complete: bool = False
    original_rejected: list[dict[str, Any]] = Field(default_factory=list)
    retained_items: list[dict[str, Any]] = Field(default_factory=list)
    repair_targets: list[dict[str, Any]] = Field(default_factory=list)
    retained_identities: list[list[Any]] = Field(default_factory=list)
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)


class Execution(Strict):
    status: Literal["ACTIVE", "DORMANT", "RESOLVED"] = "DORMANT"
    version: int = 1
    input_signature: str = "I0"
    current_binding_id: str | None = None
    blocking_review_ids: list[str] = Field(default_factory=list)
    evidence_revision: int = 0
    last_decision_signature: str | None = None
    retry_gate: str | None = None


class FactUseR2(Strict):
    use_id: str
    query_id: str
    query_version: int
    input_signature: str
    fact_id: str
    status: Literal["PENDING", "CANDIDATE", "ACCEPTED", "HELD", "REJECTED", "CONFLICT", "INVALIDATED"]
    reviewed_source_refs: list[str] = Field(default_factory=list)
    review_context_id: str | None = None
    decision_revision: int = 0
    acceptance_origin: str = "UPDATE"
    reason_code: str | None = None
    evidence_context_signature: str | None = None
    admission_origin: Literal["UPDATE", "RECALL"] | None = None
    admission_token: str | None = None
    consumed_admission_token: str | None = None


class BindingR2(Strict):
    binding_id: str
    producer_query_id: str
    query_version: int
    input_signature: str
    variable: str
    value: Any
    kind: Literal["DIRECT", "INFERRED"]
    binding_revision: int
    parent_binding_ids: list[str]
    direct_use_ids: list[str]
    direct_fact_ids: list[str]
    source_refs: list[str]
    decision_source_refs: list[str]
    supersedes_binding_id: str | None = None
    created_from_context: str
    valid: bool = True
    members: list[BindingMemberR2] = Field(default_factory=list)


class InboxEntry(Strict):
    entry_id: str
    fact_id: str
    use_key: str
    query_id: str
    lane: Literal["BIND", "REBIND"]
    update_context_id: str
    observed_status: str
    source_refs: list[str]
    evidence_revision: int
    review_id: str | None = None
    status: str = "PENDING"


class ReviewRecord(Strict):
    record_id: str
    use_key: str
    review: EvidenceReview
    context_id: str
    record_hash: str
    raw_hashes: dict[str, str]
    corrected_fact_id: str | None = None


class ReviewSession(Strict):
    review_id: str
    query_id: str
    mode: Literal["BIND", "REBIND"]
    query_version: int
    input_signature: str
    target_binding_id: str | None
    inbox_revision: int
    candidate_bucket_version: str
    evidence_signature: str
    phase: Literal["REVIEW", "FINAL"] = "REVIEW"
    status: str = "PENDING"
    required_use_ids: list[str] = Field(default_factory=list)
    staged_review_ids: dict[str, str] = Field(default_factory=dict)
    context_ids: list[str] = Field(default_factory=list)
    extra_refs: list[str] = Field(default_factory=list)
    context_requests: dict[str, ContextRequest] = Field(default_factory=dict)
    trigger: str
    budget: dict[str, int] = Field(default_factory=dict)
    sent: bool = False
    branches: list[MemberBranchR2] = Field(default_factory=list)
    branch_results: dict[str, list[BindingMemberR2]] = Field(default_factory=dict)
    branch_decisions: dict[str, str] = Field(default_factory=dict)
    completed_revision: int | None = None
    admitted_use_tokens: dict[str, str] = Field(default_factory=dict)
    prior_support_use_ids: list[str] = Field(default_factory=list)
    completion_reason: str | None = None


class DurableJob(Strict):
    job_id: str
    job_key: str
    kind: Literal["RECALL", "MEMORY", "CONTEXT_EXPAND"]
    target_query: str
    query_version: int
    input_signature: str
    review_id: str | None = None
    bucket_version: str = ""
    generation: int = 0
    status: str = "PENDING"
    lease: str | None = None
    retry_count: int = 0
    trigger_event: str
    payload: dict[str, Any] = Field(default_factory=dict)


class MemoryContextR2(Strict):
    context_id: str
    review_id: str
    state_revision: int
    allowed_mode: Literal["BIND", "REBIND"]
    phase: Literal["REVIEW", "FINAL"]
    query_id: str
    expected_cardinality: Literal["SINGLE", "SET"] | None = None
    query_version: int
    input_signature: str
    expected_binding_id: str | None
    inbox_revision: int
    candidate_bucket_version: str
    required_reviews: list[str]
    allowed_review_ids: list[str]
    allowed_fact_ids: list[str]
    visible_source_refs: list[str]
    raw_hashes: dict[str, str]
    working_memory: EvidencePackV52
    barriers: dict[str, Any]
    scope_closed: bool
    context_limits: dict[str, int]
    fact_only: bool = False
    member_bindings: bool = False
    branch: MemberBranchR2 | None = None
    admission_policy: str = "legacy"
    admission_digest: str = ""
    upstream_only_allowed: bool = False


class StateR2(Strict):
    protocol_version: Literal["v5.2-r2"] = PROTOCOL
    # Persist the evidence contract so proof invariants remain deterministic
    # across commits, replay and resume.
    fact_only: bool = False
    admission_policy: str = "legacy"
    plan: EvidencePlanR2 | QueryPlanV3
    executions: dict[str, Execution] = Field(default_factory=dict)
    binding_store: dict[str, BindingR2] = Field(default_factory=dict)
    facts: dict[str, FactNode] = Field(default_factory=dict)
    uses: dict[str, FactUseR2] = Field(default_factory=dict)
    route_index: dict[str, list[str]] = Field(default_factory=dict)
    reverse_fact_uses: dict[str, list[str]] = Field(default_factory=dict)
    inbox: dict[str, InboxEntry] = Field(default_factory=dict)
    reviews: dict[str, ReviewSession] = Field(default_factory=dict)
    review_records: dict[str, ReviewRecord] = Field(default_factory=dict)
    jobs: dict[str, DurableJob] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    processed_hints: list[str] = Field(default_factory=list)
    correction_notifications: list[str] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    query_edges: list[dict[str, Any]] = Field(default_factory=list)
    binding_ports: list[dict[str, Any]] = Field(default_factory=list)
    navigation_links: list[dict[str, Any]] = Field(default_factory=list)
    state_revision: int = 0
    scope_closed: bool = False
    read_watermark: int = -1
    cursor_state: dict[str, Any] = Field(default_factory=dict)
    pending_window: list[str] = Field(default_factory=list)
    pending_chunk: PendingChunkR2 | None = None
    update_receipts: dict[str, int] = Field(default_factory=dict)
    update_repair_progress: UpdateRepairProgressR2 | None = None
    plan_repair_failures: dict[str, PlanRepairFailureR2] = Field(default_factory=dict)
    context_expansions: int = 0
    status: str = "RUNNING"
    reason_codes: list[str] = Field(default_factory=list)
    answer: dict[str, Any] | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)

    def export(self):
        from .navigation_r2 import binding_effective
        result = {**self.model_dump(mode="json"), "bindings": {
            b.variable: b.value for b in self.binding_store.values() if binding_effective(self, b.binding_id)}}
        if isinstance(self.plan, EvidencePlanR2):
            from .member_graph_r2 import member_projection
            nodes, edges = member_projection(self)
            result["member_graph"] = dict(nodes=nodes, edges=edges, collection_scope_closed=self.scope_closed)
        return result


def use_key(qid, version, signature, fid):
    return f"{qid}|{version}|{signature}|{fid}"


def fact_id(text, refs):
    return "F" + digest([text, sorted(set(refs))])


EvidenceReview.model_rebuild()
