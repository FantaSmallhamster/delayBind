"""Small line protocols for natural-language plans, facts and bindings."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import Field, model_validator

from .schema import QueryPlan, StrictModel, Subquery


class ProtocolError(ValueError):
    pass


class FactEvent(StrictModel):
    query_id: str
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    relevance: Literal["ACTIVE", "DORMANT"]


class BindingProposal(StrictModel):
    query_id: str
    value: Any
    support_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def concrete_result(self):
        def unresolved(value: Any) -> bool:
            if isinstance(value, str):
                return not value.strip() or bool(re.fullmatch(r"\?[A-Za-z_][A-Za-z0-9_]*", value))
            if isinstance(value, list):
                return any(unresolved(item) for item in value)
            return value is None
        if unresolved(self.value):
            raise ValueError("BIND needs a concrete result, not an unresolved placeholder")
        return self


class PlanHint(StrictModel):
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


class FactUpdate(StrictModel):
    facts: list[FactEvent] = Field(default_factory=list)
    bindings: list[BindingProposal] = Field(default_factory=list)
    hints: list[PlanHint] = Field(default_factory=list)


class MemoryUpdate(StrictModel):
    bindings: list[BindingProposal] = Field(default_factory=list)
    keep: list[str] | None = None
    retract: dict[str, list[str]] = Field(default_factory=dict)
    unbind: list[str] = Field(default_factory=list)
    clear_conflicts: dict[str, list[str]] = Field(default_factory=dict)
    patches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    routes: dict[str, list[str]] = Field(default_factory=dict)


def _lines(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _refs(value: str) -> list[str]:
    refs = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not refs:
        raise ProtocolError("a source or fact reference is required")
    return refs


def _unescape(value: str) -> str:
    return re.sub(r"\\([n|\\])", lambda match: {"n": "\n", "|": "|", "\\": "\\"}[match[1]], value)


def parse_plan(raw: str) -> QueryPlan:
    """The published protocol is query blocks; JSON remains an import format."""
    if raw.lstrip().startswith("{"):
        return QueryPlan.model_validate_json(raw)
    queries: list[Subquery] = []
    current: dict[str, Any] = {}
    for line in _lines(raw):
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", line):
            if current:
                queries.append(Subquery.model_validate(current))
            current = {"id": line}
            continue
        if ":" not in line or not current:
            raise ProtocolError(f"invalid PLAN line: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        key = {"query": "template", "binds": "output"}.get(key, key)
        if key in current or key not in {"template", "output", "depends_on", "requires_complete_set", "required"}:
            raise ProtocolError(f"unknown or duplicate PLAN field: {key}")
        if key == "depends_on":
            current[key] = [] if value.upper() == "NONE" else _refs(value)
        elif key in {"requires_complete_set", "required"}:
            if value.lower() not in {"true", "false"}:
                raise ProtocolError(f"{key} must be true or false")
            current[key] = value.lower() == "true"
        else:
            current[key] = _unescape(value)
    if current:
        queries.append(Subquery.model_validate(current))
    return QueryPlan(plan_id="predicted", queries=queries)


def parse_binding(line: str) -> BindingProposal:
    fields = line.split("|", 2)
    if len(fields) != 3 or fields[0].strip() != "BIND" or "|" not in fields[2]:
        raise ProtocolError(f"invalid BIND line: {line}")
    value, refs = (item.strip() for item in fields[2].rsplit("|", 1))
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        result = _unescape(value)
    if result is None or isinstance(result, str) and not result.strip():
        raise ProtocolError("BIND needs a concrete result")
    return BindingProposal(query_id=fields[1].strip(), value=result, support_refs=_refs(refs))


def parse_update(raw: str) -> FactUpdate:
    response = FactUpdate()
    lines = _lines(raw)
    if lines == ["NONE"] or not lines:
        return response
    for line in lines:
        if line.startswith("BIND |"):
            response.bindings.append(parse_binding(line))
            continue
        if line.startswith("PLAN_HINT |"):
            fields = line.split("|", 2)
            if len(fields) != 3:
                raise ProtocolError(f"invalid PLAN_HINT: {line}")
            response.hints.append(PlanHint(source_refs=_refs(fields[1]), text=_unescape(fields[2].strip())))
            continue
        compact = re.fullmatch(r"\[([^@\]]+)\s*@\s*([^\]]+)\]\s*(.+?)\s*[（(](defer|commit)[）)]", line, re.I)
        if compact:
            query_id, refs, text, action = compact.groups()
        else:
            fields = line.split("|", 2)
            if len(fields) != 3 or "|" not in fields[2]:
                raise ProtocolError(f"invalid fact line: {line}")
            query_id, refs = fields[:2]
            text, action = fields[2].rsplit("|", 1)
        action = action.strip().upper()
        if action not in {"ACTIVE", "DORMANT", "COMMIT", "DEFER"}:
            raise ProtocolError(f"unknown fact routing marker: {action}")
        response.facts.append(FactEvent(
            query_id=query_id.strip(), source_refs=_refs(refs), text=_unescape(text.strip()),
            relevance="DORMANT" if action in {"DORMANT", "DEFER"} else "ACTIVE",
        ))
    return response


def parse_memory(raw: str) -> MemoryUpdate:
    result = MemoryUpdate()
    if not raw.strip() or raw.strip() == "NONE":
        return result
    for line in _lines(raw):
        command = line.split("|", 1)[0].strip()
        if command == "BIND":
            result.bindings.append(parse_binding(line))
            continue
        parts = [part.strip() for part in line.split("|", 2)]
        if command == "KEEP" and len(parts) == 2:
            result.keep = [] if parts[1] == "NONE" else _refs(parts[1])
        elif command == "UNBIND" and len(parts) == 2:
            result.unbind.append(parts[1])
        elif command in {"RETRACT", "CLEAR_CONFLICT", "ROUTE"} and len(parts) == 3:
            target = {"RETRACT": result.retract, "CLEAR_CONFLICT": result.clear_conflicts, "ROUTE": result.routes}[command]
            if parts[1] in target:
                raise ProtocolError(f"duplicate {command} for {parts[1]}")
            target[parts[1]] = _refs(parts[2])
        elif command == "PATCH" and len(parts) == 3:
            patch = json.loads(parts[2])
            if not isinstance(patch, dict) or parts[1] in result.patches:
                raise ProtocolError("PATCH requires one object per query")
            result.patches[parts[1]] = patch
        else:
            raise ProtocolError(f"invalid MEMORY command: {line}")
    return result


def parse_selection(raw: str, allowed: set[str]) -> set[str]:
    lines = _lines(raw)
    if lines == ["NONE"]:
        return set()
    if len(lines) != 1 or not lines[0].startswith("SELECT |"):
        raise ProtocolError("RECALL must return SELECT | fact_ids or NONE")
    selected = set(_refs(lines[0].split("|", 1)[1]))
    if not selected <= allowed:
        raise ProtocolError("RECALL selected a fact outside the candidate batch")
    return selected


def parse_judgments(raw: str, selected: set[str]) -> dict[str, str]:
    judgments: dict[str, str] = {}
    for line in _lines(raw):
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 2 or parts[1] not in {"MATCH", "MISMATCH", "UNCERTAIN", "CONFLICT"}:
            raise ProtocolError(f"invalid VERIFY judgment: {line}")
        if parts[0] in judgments:
            raise ProtocolError("duplicate VERIFY judgment")
        judgments[parts[0]] = parts[1]
    if set(judgments) != selected:
        raise ProtocolError("VERIFY must judge exactly the selected deferred candidates")
    return judgments
