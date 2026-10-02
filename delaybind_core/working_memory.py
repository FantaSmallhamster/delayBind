"""Source facts and task-specific binding links, separate from query state."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field

from .schema import StrictModel


class FactNode(StrictModel):
    fact_id: str
    text: str
    source_refs: list[str]
    observed_window: int

    @classmethod
    def create(cls, text: str, source_refs: list[str], window_index: int) -> "FactNode":
        refs = sorted(set(source_refs))
        identity = json.dumps([text, refs], ensure_ascii=False)
        return cls(fact_id="F" + hashlib.sha256(identity.encode()).hexdigest()[:16],
                   text=text, source_refs=refs, observed_window=window_index)


class FactUse(StrictModel):
    """A fact can be accepted by one query and deferred for another."""

    fact_id: str
    query_id: str
    query_version: int
    binding_version: int
    observed_window: int = 0
    status: Literal["CANDIDATE", "ACCEPTED", "INVALIDATED"]
    acceptance: Literal["COMMIT", "PROMOTE", "RECALL"] | None = None


class BindingLink(StrictModel):
    """A confirmed query binding and the proof that established it.

    Legacy candidate-association fields remain optional so old event logs can be
    replayed, but V07 never creates that kind of link.  A current link is written
    only after a BIND/REBIND has passed the runtime and source checks.
    """

    link_id: str
    query_id: str
    variable: str | None = None
    value: Any = None
    query_version: int
    binding_version: int
    resolved_version: int = 0
    kind: Literal["CONFIRMED_BINDING", "LEGACY_FACT_ASSOCIATION"] = "LEGACY_FACT_ASSOCIATION"
    rendered_query: str = ""
    effective_upstream_bindings: dict[str, Any] = Field(default_factory=dict)
    support_fact_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    upstream_query_ids: list[str] = Field(default_factory=list)
    # Pre-V07 replay compatibility only.
    upstream_fact_ids: list[str] = Field(default_factory=list)
    downstream_fact_id: str | None = None
    upstream_query_id: str | None = None


class QueryExecution(StrictModel):
    status: Literal["DORMANT", "ACTIVE", "RESOLVED"] = "DORMANT"
    result: Any = None
    support_fact_ids: list[str] = Field(default_factory=list)
    version: int = 1
    binding_version: int = 0
    resolved_version: int = 0
    activation_window: int = 0
    # A review can be pending while the query is schedulable. This is not a
    # fourth public query status.
    review_pending: bool = False
    conflicts: list[str] = Field(default_factory=list)


def render_fact_memory(facts: list[FactNode], links: list[BindingLink]) -> str:
    lines = [f"{fact.fact_id} | {','.join(fact.source_refs)} | {fact.text}" for fact in facts]
    for link in links:
        if link.kind == "CONFIRMED_BINDING":
            lines.append(
                f"CONFIRMED {link.query_id} | {link.rendered_query} | "
                f"{link.variable} = {json.dumps(link.value, ensure_ascii=False)} | "
                f"support={','.join(link.support_fact_ids)} | "
                f"inputs={json.dumps(link.effective_upstream_bindings, ensure_ascii=False, sort_keys=True)} | "
                f"version={link.query_version}:{link.binding_version}:{link.resolved_version}"
            )
        else:
            lines.append(
                f"[{','.join(link.upstream_fact_ids)}] -> {link.downstream_fact_id} | "
                f"{link.variable} = {json.dumps(link.value, ensure_ascii=False)} | {link.query_id}"
            )
    return "\n".join(lines) or "No working memory yet."
