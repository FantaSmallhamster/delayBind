"""Versioned, model-facing schemas for V5.

The schemas deliberately separate model proposals from runtime state.  A valid
JSON response is not evidence of a valid fact; provenance and deterministic
runtime checks are still required before promotion.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCHEMA_VERSION = "v1"


class Action(str, Enum):
    COMMIT = "COMMIT"
    DEFER = "DEFER"
    SKIP = "SKIP"


class Disposition(str, Enum):
    DEFERRED = "DEFERRED"
    COMMITTED = "COMMITTED"
    PROMOTED = "PROMOTED"
    SKIPPED = "SKIPPED"
    REJECTED = "REJECTED"


class EvidenceKind(str, Enum):
    RAW = "RAW"
    DERIVED = "DERIVED"


class Polarity(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"


class Modality(str, Enum):
    ASSERTED = "ASSERTED"
    POSSIBLE = "POSSIBLE"
    CONDITIONAL = "CONDITIONAL"
    REPORTED = "REPORTED"


class VerifyStatus(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    CONFLICT = "CONFLICT"
    NEED_MORE_CONTEXT = "NEED_MORE_CONTEXT"


class RuntimeStatus(str, Enum):
    RUNNING = "RUNNING"
    ANSWERED = "ANSWERED"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTED = "CONFLICTED"
    UNSUPPORTED = "UNSUPPORTED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    RUNTIME_ERROR = "RUNTIME_ERROR"


class QuestionType(str, Enum):
    COMPARISON = "comparison"
    INFERENCE = "inference"
    COMPOSITIONAL = "compositional"
    BRIDGE_COMPARISON = "bridge-comparison"


class QueryNodeKind(str, Enum):
    CONSTANT = "CONSTANT"
    VARIABLE = "VARIABLE"
    ANSWER = "ANSWER"


class QueryEdgeStatus(str, Enum):
    DORMANT = "DORMANT"
    ACTIVE = "ACTIVE"
    SATISFIED = "SATISFIED"
    CONFLICTED = "CONFLICTED"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pattern(StrictModel):
    id: str
    subject: str
    relation: str
    object: str
    relation_family: str | None = None
    cardinality: Literal["SINGLE", "OPTIONAL_SINGLE", "SET"] = "SINGLE"
    required: bool = True
    qualifiers: dict[str, Any] = Field(default_factory=dict)

    @property
    def relation_key(self) -> str:
        return self.relation_family or self.relation


class RelationSpec(StrictModel):
    id: str
    description: str
    relation_family: str | None = None
    subject_type: str | None = None
    object_type: str | None = None
    direction: str | None = None
    cardinality: Literal["SINGLE", "OPTIONAL_SINGLE", "SET"] = "SINGLE"
    realizations: list[dict[str, Any]] = Field(default_factory=list, max_length=3)


class OperatorSpec(StrictModel):
    id: str
    type: str
    # Inputs may be variables/literals or nested lists for SET operations.
    inputs: list[Any] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)


class AnswerContract(StrictModel):
    target: str | None = None
    type: Literal[
        "ENTITY",
        "BOOLEAN",
        "NUMBER",
        "DATE",
        "COUNTRY",
        "NATIONALITY",
        "LOCATION",
        "ENTITY_SET",
        "SHORT_TEXT",
    ]
    cardinality: Literal["SINGLE", "SET"] = "SINGLE"
    normalization: str = "IDENTITY"


class QuestionContract(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    sample_id: str
    question_type: str | None = None
    required_ops: list[str] = Field(default_factory=list)
    answer_contract: AnswerContract | None = None
    evidence_cardinality: Literal["SINGLE", "SET", "UNKNOWN"] = "UNKNOWN"
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryPlan(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    plan_id: str
    plan_version: int = 1
    patterns: list[Pattern] = Field(min_length=1, max_length=16)
    relation_specs: list[RelationSpec] = Field(default_factory=list, max_length=16)
    operators: list[OperatorSpec] = Field(default_factory=list, max_length=8)
    constraints: dict[str, Any] = Field(default_factory=dict)
    answer_contract: AnswerContract | None = None


class QueryNode(StrictModel):
    id: str
    kind: QueryNodeKind
    symbol: str
    value_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class QueryEdge(StrictModel):
    id: str
    subject_node_id: str
    relation: str
    object_node_id: str
    relation_family: str | None = None
    cardinality: Literal["SINGLE", "OPTIONAL_SINGLE", "SET"] = "SINGLE"
    required: bool = True
    qualifiers: dict[str, Any] = Field(default_factory=dict)

    @property
    def relation_key(self) -> str:
        return self.relation_family or self.relation


class QueryOperatorNode(StrictModel):
    id: str
    operator_type: str
    input_node_ids: list[str] = Field(default_factory=list)
    output_node_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class OpenQueryGraph(StrictModel):
    """Question-conditioned graph of evidence requirements.

    This is a demand graph, not a memory graph: constants and open variables
    describe what evidence must be found while reading the stream.
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    graph_id: str
    nodes: list[QueryNode] = Field(min_length=1, max_length=64)
    edges: list[QueryEdge] = Field(min_length=1, max_length=16)
    operator_nodes: list[QueryOperatorNode] = Field(default_factory=list, max_length=8)
    answer_node_id: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)


class Value(StrictModel):
    value_type: Literal["ENTITY", "DATE", "YEAR", "NUMBER", "TEXT", "BOOLEAN"]
    raw_text: str | None = None
    normalized: Any
    precision: str | None = None
    unit: str | None = None


class TripleEvent(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    event_id: str
    source_ref: str
    subject: str
    concrete_relation: str
    object: Any
    matched_family: str | None = None
    pattern_hint: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    polarity: Polarity = Polarity.POSITIVE
    modality: Modality = Modality.ASSERTED
    proposed_action: Action = Action.DEFER
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    span_hint: str | None = None
    source_order: int | None = None


class UpdateResponse(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    events: list[TripleEvent] = Field(default_factory=list)
    has_more: bool = False


class EdgeFillMatch(StrictModel):
    source_ref: str
    subject: str
    object: Any
    supporting_text: str = Field(min_length=1, max_length=800)


class EdgeFillResponse(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    matches: list[EdgeFillMatch] = Field(default_factory=list, max_length=8)


class AnswerResponse(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    answer: Any = None
    answer_type: str | None = None
    source_refs: list[str] = Field(default_factory=list)


class EvidenceAssertion(StrictModel):
    source_ref: str
    stance: Literal["SUPPORTS", "CONTRADICTS"] = "SUPPORTS"
    verified: bool = False
    raw_text: str | None = None


class Claim(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    claim_id: str
    subject: str
    relation: str
    object: Any
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    polarity: Polarity = Polarity.POSITIVE
    modality: Modality = Modality.ASSERTED
    evidence_kind: EvidenceKind = EvidenceKind.RAW
    disposition: Disposition = Disposition.DEFERRED
    verification: VerifyStatus | None = None
    evidence_assertions: list[EvidenceAssertion] = Field(default_factory=list)
    parent_claim_ids: list[str] = Field(default_factory=list)
    operator_metadata: dict[str, Any] = Field(default_factory=dict)
    matched_pattern_id: str | None = None

    @property
    def claim_key(self) -> tuple[Any, ...]:
        qualifiers = tuple(sorted((str(k), repr(v)) for k, v in self.qualifiers.items()))
        return (
            self.subject.casefold().strip(),
            self.relation.casefold().strip(),
            repr(self.object),
            qualifiers,
            self.polarity.value,
            self.modality.value,
        )


class VerifyResult(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    claim_id: str
    status: VerifyStatus
    normalized_claim: Claim | None = None
    reason: str | None = None
    expanded_source_refs: list[str] = Field(default_factory=list)
    supporting_text: str | None = None


class VerifyDecision(StrictModel):
    """Compact model-facing decision for one verification attempt.

    ``VerifyResult`` is the Runtime's richer internal record.  Sending its
    complete JSON Schema to a verifier also sends the nested Claim schema,
    which is unnecessary for a three-way decision and measurably bloats the
    prompt.  This DTO deliberately keeps the model interface small.
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    claim_id: str
    status: VerifyStatus
    reason: str = Field(min_length=1, max_length=480)
    supporting_source_refs: list[str] = Field(default_factory=list, max_length=3)
    supporting_text: str | None = Field(default=None, max_length=480)

    @model_validator(mode="after")
    def require_support_for_accept(self) -> "VerifyDecision":
        if self.status == VerifyStatus.ACCEPT:
            if not self.supporting_source_refs:
                raise ValueError("ACCEPT requires at least one supporting_source_ref")
            if not self.supporting_text or not self.supporting_text.strip():
                raise ValueError("ACCEPT requires supporting_text copied from the cited source")
        return self


class EvidencePack(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    claims: list[Claim] = Field(default_factory=list)
    operator_trace: list[dict[str, Any]] = Field(default_factory=list)
    answer_value: Any = None
    answer_type: str | None = None
    query_graph: dict[str, Any] | None = None
    support_mapping: dict[str, list[str]] = Field(default_factory=dict)


class RuntimeEvent(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    event_id: str
    run_id: str
    event_seq: int | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_ref: str | None = None
    stream_position: int | None = None
    caused_by_event_seq: int | None = None
    transaction_id: str | None = None


class ModelCall(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    call_id: str
    run_id: str
    interface: Literal["PLAN", "UPDATE", "VERIFY", "ANSWER"]
    request_hash: str
    model: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    prompt_hash: str
    raw_request: Any = None
    raw_response: Any = None
    parsed_output: Any = None
    attempt: int = 1
    cache_hit: bool = False
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class RunResult(StrictModel):
    status: RuntimeStatus
    reason_codes: list[str] = Field(default_factory=list)
    evidence_pack: EvidencePack | None = None
