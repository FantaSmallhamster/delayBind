from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..data import CanonicalSample, CanonicalDocument


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Explicit JSON types keep the value contract visible to constrained decoders.
# Nested member combinations are not supported by this runtime.
Scalar = str | int | float | bool
ResultValue = Scalar | list[Scalar] | dict[str, Scalar] | None


class InferenceInput(Strict):
    sample_id: str
    question: str
    documents: list[CanonicalDocument] = Field(default_factory=list)
    context: str | None = None

    @classmethod
    def from_sample(cls, sample: CanonicalSample):
        return cls(sample_id=sample.sample_id, question=sample.question,
                   documents=sample.documents, context=sample.context)


class EvalAnnotations(Strict):
    answers: list[str] = Field(default_factory=list)
    supporting_facts: list[tuple[str, int]] = Field(default_factory=list)
    evidences: list[list[Any]] = Field(default_factory=list)

    @classmethod
    def from_sample(cls, sample):
        return cls(answers=sample.answers or ([sample.answer] if sample.answer else []),
                   supporting_facts=sample.supporting_facts, evidences=sample.evidences)


class SourceSpan(Strict):
    doc_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    source_hash: str


class EvidenceRecord(Strict):
    evidence_id: str
    source_spans: list[SourceSpan] = Field(min_length=1)
    cue_text: str = Field(min_length=1)
    entity_mentions: list[str] = Field(default_factory=list)
    relation_cues: list[str] = Field(default_factory=list)
    candidate_query_ids: list[str] = Field(default_factory=list)
    observed_window: int
    record_version: int = 1


class Proposal(Strict):
    source_refs: list[str] = Field(min_length=1)
    cue_text: str = Field(min_length=1)
    entity_mentions: list[str] = Field(default_factory=list)
    relation_cues: list[str] = Field(default_factory=list)
    candidate_query_ids: list[str] = Field(default_factory=list)


class Update(Strict):
    evidence: list[Proposal] = Field(default_factory=list, description="Source-backed facts to remember. Put every retained factual cue here, with source_refs from the current window.")
    plan_notes: list[str] = Field(default_factory=list, description="Optional short strategy notes only; facts here have no source access and cannot answer the question.")


class Demand(Strict):
    id: str
    template: str = Field(max_length=400, description="One concise natural-language subquestion, without reasoning. Use literal known names and ?variables for unknown inputs produced by ancestors; never braces or dollar variables.")
    output: str = Field(pattern=r"^\?[A-Za-z_][A-Za-z0-9_]*$", description="Unique output variable such as ?person, not an answer or question.")
    depends_on: list[str] = Field(default_factory=list, description="IDs of queries whose outputs this query needs.")
    relation_cues: list[str] = Field(default_factory=list)
    requires_complete_set: bool = False


class Plan(Strict):
    queries: list[Demand] = Field(default_factory=list)
    version: int = 1


class Assessment(Strict):
    input_fingerprint: str
    status: Literal["SUPPORTED", "CONTRADICTED", "INSUFFICIENT"] = Field(description="Whether raw sources establish an answer to the concrete question, independently of cue fidelity.")
    value: ResultValue = Field(default=None, description="Answer to the concrete question from raw sources; null if unsupported. For a yes/no question use Yes or No.")
    support_ids: list[str] = Field(default_factory=list, description="Candidate evidence IDs whose linked raw sources support value, even when their cue text was inaccurate.")
    faithful: bool = Field(default=True, description="Whether the candidate cues are faithful to raw sources. Can be false while status is SUPPORTED and value is corrected from raw sources.")
    qualifiers: list[str] = Field(default_factory=list)
    coverage: Literal["UNKNOWN", "PARTIAL"] = "UNKNOWN"

    @model_validator(mode='after')
    def supported_requires_sources(self):
        if self.status=='SUPPORTED' and (self.value is None or not self.support_ids):
            raise ValueError('SUPPORTED requires a non-null value and nonempty candidate support_ids')
        return self


class Binding(Strict):
    query_id: str
    input_fingerprint: str
    value: Any
    support_ids: list[str]
    parent_versions: dict[str, int] = Field(default_factory=dict)
    version: int
    qualifiers: list[str] = Field(default_factory=list)
    coverage: Literal["UNKNOWN", "PARTIAL"] = "UNKNOWN"


class LiveState(Strict):
    plan: Plan = Field(default_factory=Plan)
    evidence: dict[str, EvidenceRecord] = Field(default_factory=dict)
    bindings: dict[str, Binding] = Field(default_factory=dict)
    # Only current-instance assessments; historical judgments are audit only.
    assessments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    plan_notes: list[str] = Field(default_factory=list)
    version: int = 0
    status: str = "READING"


class Answer(Strict):
    status: Literal["ANSWERED", "INSUFFICIENT"]
    answer: ResultValue = Field(default=None, description="Concise answer from displayed raw sources when ANSWERED; null only when INSUFFICIENT.")
    source_refs: list[str] = Field(default_factory=list, description="Displayed raw source references supporting the answer; required and nonempty when ANSWERED.")

    @model_validator(mode='after')
    def answered_requires_sources(self):
        if self.status=='ANSWERED' and (self.answer is None or not self.source_refs):
            raise ValueError('ANSWERED requires a non-null answer and nonempty source_refs')
        return self
