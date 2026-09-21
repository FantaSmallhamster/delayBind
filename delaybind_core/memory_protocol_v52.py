"""Strict incremental MEMORY proposals; operation order confers no authority."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .schema_v52 import Strict


class Correction(Strict):
    local_id: str = Field(pattern=r"^N[0-9]+$")
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


class ContextRequest(Strict):
    anchor: str
    before: int = Field(default=1, ge=0, strict=True)
    after: int = Field(default=1, ge=0, strict=True)


class Assess(Strict):
    op: Literal["ASSESS"]
    query_id: str
    fact_id: str
    verdict: Literal["ACCEPT", "REJECT", "HOLD", "CONFLICT"]
    checked_refs: list[str] = Field(min_length=1)
    reason_code: str = Field(min_length=1, max_length=80)
    correction: Correction | None = None
    context_request: ContextRequest | None = None

    @model_validator(mode="after")
    def validate_options(self):
        if self.correction and self.verdict != "ACCEPT":
            raise ValueError("correction requires ACCEPT")
        if self.context_request and self.verdict != "HOLD":
            raise ValueError("context_request requires HOLD")
        return self


class Bind(Strict):
    op: Literal["BIND"]
    query_id: str
    value: Any
    kind: Literal["DIRECT", "INFERRED"]
    support_fact_ids: list[str]


class Unbind(Strict):
    op: Literal["UNBIND"]
    query_id: str
    reason_code: str = Field(min_length=1, max_length=80)
    evidence_fact_ids: list[str]


class QueryPatch(Strict):
    template: str | None = None
    output: str | None = None
    inputs: dict[str, str] | None = None
    cardinality: Literal["SINGLE", "SET"] | None = None
    requires_complete_set: bool | None = None


class Patch(Strict):
    op: Literal["PATCH"]
    query_id: str
    patch: QueryPatch
    evidence_fact_ids: list[str]


class Route(Strict):
    op: Literal["ROUTE"]
    query_id: str
    fact_ids: list[str] = Field(min_length=1)


class Focus(Strict):
    op: Literal["FOCUS"]
    fact_ids: list[str]


Operation = Annotated[Assess | Bind | Unbind | Patch | Route | Focus, Field(discriminator="op")]


class MemoryProposal(Strict):
    schema_version: Literal["memory-v5.2"]
    context_id: str
    operations: list[Operation]

    @model_validator(mode="after")
    def reject_duplicates(self):
        seen, local_ids = set(), set()
        for op in self.operations:
            key = (("RESULT", op.query_id) if isinstance(op, (Bind, Unbind))
                   else (op.op, op.query_id, op.fact_id) if isinstance(op, Assess)
                   else (op.op, op.query_id) if isinstance(op, Patch)
                   else ("FOCUS",) if isinstance(op, Focus) else None)
            if key is not None:
                if key in seen:
                    raise ValueError(f"DUPLICATE_OPERATION:{key}")
                seen.add(key)
            if isinstance(op, Assess) and op.correction:
                if op.correction.local_id in local_ids:
                    raise ValueError("DUPLICATE_LOCAL_ID")
                local_ids.add(op.correction.local_id)
        return self
