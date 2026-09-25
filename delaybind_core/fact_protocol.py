"""Small line protocols for natural-language plans, facts and bindings."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

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
    ignored_lines: list[dict[str, str]] = Field(default_factory=list)
    rejected_lines: list[dict[str, str]] = Field(default_factory=list)
    query_id_mappings: dict[str, str] = Field(default_factory=dict)


class MemoryUpdate(StrictModel):
    bindings: list[BindingProposal] = Field(default_factory=list)
    rebindings: list[BindingProposal] = Field(default_factory=list)
    ignored_lines: list[dict[str, str]] = Field(default_factory=list)
    rejected_lines: list[dict[str, str]] = Field(default_factory=list)


def _lines(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        if "\n" not in raw:
            raise ProtocolError("empty or invalid fenced response")
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return [re.sub(r"^[-*]\s+", "", line.strip()) for line in raw.splitlines() if line.strip()]


def clean_identifier(value: str) -> str:
    value = value.strip()
    for left, right in (("`", "`"), ('"', '"'), ("'", "'")):
        if len(value) >= 2 and value.startswith(left) and value.endswith(right):
            value = value[1:-1].strip()
    return value


def resolve_query_id(value: str, allowed: set[str]) -> str:
    """Normalize only unambiguous spelling/decoration of a declared query."""
    if value in allowed:
        return value
    value = clean_identifier(value)
    candidates = [value]
    annotation = re.fullmatch(r"(.+)@(?:NONE|NULL)", value, re.I)
    if annotation:
        candidates.append(annotation[1])
    status_annotation = re.fullmatch(r"(.+?)\s*\[(?:ACTIVE|DORMANT|RESOLVED)\]", value, re.I)
    if status_annotation:
        candidates.append(clean_identifier(status_annotation[1]))
    for candidate in candidates:
        matches = [query_id for query_id in allowed if query_id.casefold() == candidate.casefold()]
        if len(matches) == 1:
            return matches[0]
    raise ProtocolError(f"unknown query ID {value!r}; use one of {', '.join(sorted(allowed))}")


def _command_line(line: str) -> str:
    match = re.match(r"^(BIND|REBIND|PLAN_HINT)(?:\s*\|\s*|\s+)(.*)$", line, re.I)
    return f"{match[1].upper()} | {match[2]}" if match else line


def _refs(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("["):
        try:
            values = json.loads(value)
        except json.JSONDecodeError:
            if not value.endswith("]"):
                raise ProtocolError("unclosed reference list")
            values = value[1:-1].split(",")
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ProtocolError("references must be strings")
    else:
        values = value.split(",")
    refs = list(dict.fromkeys(clean_identifier(part) for part in values if clean_identifier(part)))
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


def parse_binding(line: str, *, command: str = "BIND") -> BindingProposal:
    fields = line.split("|", 2)
    command = command.upper()
    if command not in {"BIND", "REBIND"}:
        raise ProtocolError(f"unsupported binding command: {command}")
    if len(fields) != 3 or fields[0].strip().upper() != command or "|" not in fields[2]:
        raise ProtocolError(f"invalid {command} line: {line}")
    value, refs = (item.strip() for item in fields[2].rsplit("|", 1))
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        result = _unescape(value)
    if result is None or isinstance(result, str) and not result.strip():
        raise ProtocolError(f"{command} needs a concrete result")
    return BindingProposal(query_id=fields[1].strip(), value=result, support_refs=_refs(refs))


def parse_update(raw: str, *, recover: bool = False) -> FactUpdate:
    if recover:
        result = FactUpdate()
        for line in _lines(raw):
            try:
                part = parse_update(line)
                for fact in part.facts:
                    if any(ref.upper() in {"NONE", "NULL"} for ref in fact.source_refs):
                        result.rejected_lines.append({"line": line, "reason": "MISSING_SOURCE: omit facts without a cited source"})
                    else:
                        result.facts.append(fact)
                result.bindings.extend(part.bindings)
                result.hints.extend(part.hints)
                result.ignored_lines.extend(part.ignored_lines)
            except (ValueError, ValidationError) as exc:
                result.rejected_lines.append({"line": line, "reason": str(exc)})
        return result
    response = FactUpdate()
    lines = _lines(raw)
    if not lines or (len(lines) == 1 and lines[0].upper() == "NONE"):
        return response
    for line in lines:
        line = _command_line(line)
        fields = [field.strip() for field in line.split("|")]
        template = [field.casefold() for field in fields]
        if template == ["query_id", "source_ref1,source_ref2", "complete natural-language fact", "active or dormant"]:
            response.ignored_lines.append({"line": line, "reason": "COPIED_FORMAT_HEADER"})
            continue
        if line == "NONE" or (len(fields) == 4 and fields[0] == "NONE" and fields[1] in {"", "NONE"}):
            response.ignored_lines.append({"line": line, "reason": "NO_EVIDENCE_PLACEHOLDER"})
            continue
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
        text = _unescape(text.strip())
        # Only source-free, explicit reports of no evidence are no-ops. A
        # cited negative fact must survive; an unsupported assertion must fail.
        if refs.strip().upper() in {"", "NONE", "NULL", "[]"}:
            if text.upper() in {"", "NONE", "NULL"}:
                response.ignored_lines.append({"line": line, "reason": "NO_EVIDENCE_PLACEHOLDER"})
                continue
            if re.fullmatch(
                r"(?:No (?:document|source) (?:provides|states|lists|contains) [^.!?;]+"
                r"|Cannot determine [^.!?;]+ (?:without [^.!?;]+"
                r"|because [^.!?;]+ (?:is|are) (?:unknown|missing|not provided)))\.?",
                text, re.I,
            ):
                response.ignored_lines.append({"line": line, "reason": "NO_EVIDENCE_REPORT"})
                continue
        response.facts.append(FactEvent(
            query_id=query_id.strip(), source_refs=_refs(refs), text=text,
            relevance="DORMANT" if action in {"DORMANT", "DEFER"} else "ACTIVE",
        ))
    return response


def parse_memory(raw: str, *, recover: bool = False) -> MemoryUpdate:
    if recover:
        result = MemoryUpdate()
        in_note = False
        for line in _lines(raw):
            if re.fullmatch(r"(?:note|notes|explanation)\s*:", line, re.I):
                in_note = True
                result.ignored_lines.append({"line": line, "reason": "EXPLANATION"})
                continue
            command = _command_line(line)
            if in_note and not re.match(r"^(BIND|REBIND) \|", command):
                result.ignored_lines.append({"line": line, "reason": "EXPLANATION"})
                continue
            in_note = False
            try:
                part = parse_memory(command)
                result.bindings.extend(part.bindings)
                result.rebindings.extend(part.rebindings)
            except (ValueError, ValidationError) as exc:
                result.rejected_lines.append({"line": line, "reason": str(exc)})
        return result
    result = MemoryUpdate()
    if not raw.strip() or raw.strip().upper() == "NONE":
        return result
    for line in _lines(raw):
        line = _command_line(line)
        command = line.split("|", 1)[0].strip()
        if command in {"BIND", "REBIND"}:
            target = result.bindings if command == "BIND" else result.rebindings
            target.append(parse_binding(line, command=command))
            continue
        raise ProtocolError(f"invalid MEMORY command: {line}; only BIND and REBIND are allowed")
    return result


def normalize_memory_queries(update: MemoryUpdate, allowed: set[str]) -> MemoryUpdate:
    result = update.model_copy(deep=True)
    result.bindings = []
    result.rebindings = []
    for binding in update.bindings:
        try:
            query_id = resolve_query_id(binding.query_id, allowed)
            if any(ref.upper() in {"NONE", "NULL"} for ref in binding.support_refs):
                raise ProtocolError("BIND requires accepted supporting fact IDs; omit unsupported bindings")
            result.bindings.append(binding.model_copy(update={"query_id": query_id}))
        except ProtocolError as exc:
            result.rejected_lines.append({"line": f"BIND | {binding.query_id} | {binding.value} | {','.join(binding.support_refs)}", "reason": str(exc)})
    for binding in update.rebindings:
        try:
            query_id = resolve_query_id(binding.query_id, allowed)
            if any(ref.upper() in {"NONE", "NULL"} for ref in binding.support_refs):
                raise ProtocolError("REBIND requires accepted supporting fact IDs; omit unsupported bindings")
            result.rebindings.append(binding.model_copy(update={"query_id": query_id}))
        except ProtocolError as exc:
            result.rejected_lines.append({"line": f"REBIND | {binding.query_id} | {binding.value} | {','.join(binding.support_refs)}", "reason": str(exc)})
    return result


def parse_selection(raw: str, allowed: set[str], *, context_ids: set[str] | None = None,
                    ignored_context: set[str] | None = None) -> set[str]:
    lines = [line for line in _lines(raw) if line.replace(" ", "").casefold() != "fact_id1,fact_id2"]
    value = ",".join(lines)
    prefix = re.match(r"^SELECT(?:\s*\|\s*|\s*:\s*|\s+)(.*)$", value, re.I)
    if prefix:
        value = prefix[1]
    if value.upper() == "NONE" or value == "[]":
        return set()
    selected = set(_refs(value))
    outside = selected - allowed
    if not outside <= (context_ids or set()):
        raise ProtocolError("RECALL selected a fact outside the candidate batch: " + ",".join(sorted(outside - (context_ids or set()))))
    if ignored_context is not None:
        ignored_context.update(outside)
    return selected & allowed


def parse_judgments(raw: str, selected: set[str]) -> dict[str, str]:
    judgments: dict[str, str] = {}
    for line in _lines(raw):
        if line.casefold().replace(" ", "") in {"fact_id|verdict", "fact_id|判定"}:
            continue
        match = re.fullmatch(r"(.+?)(?:\s*\|\s*|\s*:\s*|\s+)(MATCH|MISMATCH|UNCERTAIN|CONFLICT)", line, re.I)
        if not match:
            raise ProtocolError(f"invalid VERIFY judgment: {line}")
        fact_id, verdict = clean_identifier(match[1]), match[2].upper()
        if fact_id in judgments and judgments[fact_id] != verdict:
            raise ProtocolError("conflicting duplicate VERIFY judgments")
        judgments[fact_id] = verdict
    if set(judgments) != selected:
        raise ProtocolError("VERIFY must judge exactly the selected deferred candidates")
    return judgments
