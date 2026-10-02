"""Model-facing and persisted schemas used by V08."""
from __future__ import annotations
from enum import Enum
from typing import Any, Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

SCHEMA_VERSION = "v1"


class RuntimeStatus(str, Enum):
    RUNNING = "RUNNING"
    ANSWERED = "ANSWERED"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTED = "CONFLICTED"
    UNSUPPORTED = "UNSUPPORTED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    RUNTIME_ERROR = "RUNTIME_ERROR"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubqueryStatus(str, Enum):
    DORMANT = "DORMANT"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class Subquery(StrictModel):
    id: str = Field(min_length=1)
    template: str = Field(min_length=1, validation_alias=AliasChoices("template", "query"))
    depends_on: list[str] = Field(default_factory=list)
    output: str = Field(validation_alias=AliasChoices("output", "binds"))
    required: bool = True
    requires_complete_set: bool = False

    @property
    def query(self) -> str:
        return self.template

    @property
    def binds(self) -> str:
        return self.output


class QueryPlan(StrictModel):
    """Natural-language subqueries; execution state is owned by Runtime."""

    schema_version: Literal["v2"] = "v2"
    plan_id: str = Field(min_length=1)
    queries: list[Subquery] = Field(min_length=1)
    # When omitted, the last query supplies the final answer.
    answer_query_id: str | None = None


class AnswerResponse(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    answer: Any = None
    answer_type: str | None = None
    source_refs: list[str] = Field(default_factory=list)


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
    interface: Literal["PLAN", "UPDATE", "MEMORY", "MEMORY_VERIFY", "MEMORY_GROUNDED", "RECALL", "VERIFY", "ANSWER"]
    agent_role: Literal["HIGH", "LOW"] | None = None
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
