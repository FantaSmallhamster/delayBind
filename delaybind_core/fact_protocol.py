"""Small line protocols for natural-language plans, facts and bindings."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from .schema import QueryPlan, StrictModel, Subquery


class ProtocolError(ValueError):
    pass


class FactEvent(StrictModel):
    query_id: str
    text: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


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


class SourceCheck(StrictModel):
    query_id: str
    fact_id: str
    verdict: Literal["SUPPORTED", "SOURCE_DIFF", "UNRESOLVED"]
    source_refs: list[str] = Field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class SourceCheckResult:
    checks: tuple[SourceCheck, ...]
    rejected_lines: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class GroundedMemoryResult:
    checks: SourceCheckResult
    memory: MemoryUpdate


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


def _update_fields(line: str) -> list[str]:
    """Split the three-column protocol without splitting escaped literal pipes."""
    fields: list[str] = []
    start = 0
    escaped = False
    for index, char in enumerate(line):
        if char == "|" and not escaped:
            fields.append(line[start:index].strip())
            start = index + 1
        escaped = char == "\\" and not escaped
    fields.append(line[start:].strip())
    return fields


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
        fields = _update_fields(line)
        template = [field.casefold() for field in fields]
        if template == ["query_id", "source_ref1,source_ref2", "complete natural-language fact"]:
            response.ignored_lines.append({"line": line, "reason": "COPIED_FORMAT_HEADER"})
            continue
        if line == "NONE" or (len(fields) == 3 and fields[0] == "NONE" and fields[1] in {"", "NONE"}):
            response.ignored_lines.append({"line": line, "reason": "NO_EVIDENCE_PLACEHOLDER"})
            continue
        if line.startswith("BIND |"):
            response.bindings.append(parse_binding(line))
            continue
        if line.startswith("PLAN_HINT |"):
            if len(fields) != 3:
                raise ProtocolError(f"invalid PLAN_HINT: {line}")
            response.hints.append(PlanHint(source_refs=_refs(fields[1]), text=_unescape(fields[2].strip())))
            continue
        if len(fields) != 3:
            raise ProtocolError(f"UPDATE requires exactly three fields (query | sources | fact), without a state: {line}")
        query_id, refs, text = fields
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
                    ignored_context: set[str] | None = None,
                    recover: bool = False) -> set[str]:
    lines = [line for line in _lines(raw) if line.replace(" ", "").casefold() != "fact_id1,fact_id2"]
    value = ",".join(lines)
    prefix = re.match(r"^SELECT(?:\s*\|\s*|\s*:\s*|\s+)(.*)$", value, re.I)
    if prefix:
        value = prefix[1]
    if value.upper() == "NONE" or value == "[]":
        return set()
    selected = set(_refs(value))
    outside = selected - allowed
    unsupported = outside - (context_ids or set())
    if unsupported and not recover:
        raise ProtocolError("RECALL selected a fact outside the candidate batch: " + ",".join(sorted(unsupported)))
    if ignored_context is not None:
        ignored_context.update(outside)
    return selected & allowed


def parse_verification(raw: str, proposed_query_ids: set[str]) -> dict[str, str]:
    """Parse MEMORY_VERIFY output: query_id | SUPPORT|CONTRADICT|INSUFFICIENT | reason."""
    verdicts: dict[str, str] = {}
    if not raw.strip() or raw.strip().upper() == "NONE":
        return verdicts
    for line in _lines(raw):
        fields = [f.strip() for f in line.split("|", 2)]
        if len(fields) < 2:
            raise ProtocolError(f"invalid verification line: {line}")
        qid = clean_identifier(fields[0])
        verdict = fields[1].upper()
        if verdict not in {"SUPPORT", "CONTRADICT", "INSUFFICIENT"}:
            raise ProtocolError(f"invalid verification verdict {verdict!r}; use SUPPORT, CONTRADICT, or INSUFFICIENT")
        if qid not in proposed_query_ids:
            raise ProtocolError(f"verification for unknown query {qid!r}; only verify proposed IDs")
        if qid in verdicts and verdicts[qid] != verdict:
            raise ProtocolError(f"conflicting duplicate verification for {qid}")
        verdicts[qid] = verdict
    return verdicts


def parse_verification_recover(raw: str, proposed_query_ids: set[str]) -> dict[str, str]:
    """Recover explicit MEMORY_VERIFY verdict rows from a verbose response.

    Qwen occasionally prefixes a valid verdict with a short explanation or
    appends a self-correction paragraph.  The strict parser correctly rejects
    that whole response, even when it contains an unambiguous ``Q1 | SUPPORT``
    row.  This recovery parser accepts only rows that contain exactly one
    proposed query ID and one explicit verdict token; all prose is ignored.
    It never fabricates a verdict for a query that was not stated explicitly.
    """
    try:
        return parse_verification(raw, proposed_query_ids)
    except ProtocolError:
        pass

    verdict_aliases = {
        "SUPPORT": "SUPPORT", "SUPPORTED": "SUPPORT",
        "CONTRADICT": "CONTRADICT", "CONTRADICTED": "CONTRADICT",
        "INSUFFICIENT": "INSUFFICIENT",
    }
    qids = sorted(proposed_query_ids, key=len, reverse=True)
    recovered: dict[str, str] = {}
    conflicted: set[str] = set()
    for line in _lines(raw):
        # A model explanation can mention a query and the word "support" but
        # still not be a verdict row.  Require a protocol separator when the
        # line contains additional prose.
        row_match = re.match(r"^\s*(?:(?:[-*]|\d+[.)])\s*)?([^|:]+?)\s*(?:\||:|\s+)", line)
        if not row_match:
            continue
        row_id = clean_identifier(row_match[1])
        candidates = [qid for qid in qids if row_id.casefold() == qid.casefold()]
        verdict_tokens = [token for token in verdict_aliases
                          if re.search(rf"(?<![A-Za-z]){token}(?![A-Za-z])", line[row_match.end():], re.I)]
        if len(candidates) != 1 or len(verdict_tokens) != 1:
            continue
        if "|" not in line and ":" not in line:
            continue
        qid = candidates[0]
        verdict = verdict_aliases[verdict_tokens[0]]
        if qid in conflicted:
            continue
        if qid in recovered and recovered[qid] != verdict:
            recovered.pop(qid, None)
            conflicted.add(qid)
            continue
        recovered[qid] = verdict
    return recovered


def parse_source_checks(raw: str, allowed_refs: dict[tuple[str, str], set[str]]) -> SourceCheckResult:
    """Fail closed per query/fact pair while retaining independent valid rows."""
    checks: dict[tuple[str, str], SourceCheck] = {}
    rejected: list[dict[str, str]] = []
    conflicted: set[tuple[str, str]] = set()
    for line in _lines(raw):
        fields = [field.strip() for field in line.split("|", 4)]
        if len(fields) != 5:
            rejected.append({"line": line, "reason": "source check requires five fields"})
            if len(fields) >= 2:
                key = (clean_identifier(fields[0]), clean_identifier(fields[1]))
                if key in allowed_refs:
                    conflicted.add(key)
            continue
        qid, fid, verdict = fields[:3]
        key = (clean_identifier(qid), clean_identifier(fid))
        if key not in allowed_refs:
            rejected.append({"line": line, "reason": "unknown source-check query/fact pair"})
            continue
        verdict = verdict.upper()
        if verdict not in {"SUPPORTED", "SOURCE_DIFF", "UNRESOLVED"}:
            rejected.append({"line": line, "reason": "unknown source-check verdict"})
            conflicted.add(key)
            continue
        try:
            refs = [] if fields[3].upper() in {"", "NONE", "NULL"} else _refs(fields[3])
        except ProtocolError as exc:
            rejected.append({"line": line, "reason": str(exc)})
            conflicted.add(key)
            continue
        if not set(refs) <= allowed_refs[key] or (verdict != "UNRESOLVED" and not refs):
            rejected.append({"line": line, "reason": "missing or out-of-scope source reference"})
            conflicted.add(key)
            continue
        check = SourceCheck(query_id=key[0], fact_id=key[1], verdict=verdict,
                            source_refs=refs, note=fields[4] if len(fields) == 5 else "")
        prior = checks.get(key)
        if prior and (prior.verdict != check.verdict or prior.source_refs != check.source_refs):
            rejected.append({"line": line, "reason": "conflicting duplicate source check"})
            conflicted.add(key)
        else:
            checks[key] = check
    for key in allowed_refs:
        if key not in checks or key in conflicted:
            checks[key] = SourceCheck(query_id=key[0], fact_id=key[1], verdict="UNRESOLVED")
    return SourceCheckResult(
        checks=tuple(checks[key] for key in sorted(checks)),
        rejected_lines=tuple(rejected),
    )
def parse_grounded_memory(raw: str, allowed_refs: dict[tuple[str, str], set[str]],
                          query_ids: set[str]) -> GroundedMemoryResult:
    """Parse both sections before any binding can be considered for admission."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    malformed = False
    for line in _lines(raw):
        header = line.rstrip(":").upper()
        if header in {"CHECKS", "BINDINGS"}:
            if header in sections:
                malformed = True
            current = header
            sections.setdefault(header, [])
        elif current is None:
            malformed = True
        else:
            sections[current].append(line)
    if malformed or set(sections) != {"CHECKS", "BINDINGS"}:
        checks = parse_source_checks("", allowed_refs)
        return GroundedMemoryResult(checks, MemoryUpdate(rejected_lines=[{
            "line": raw, "reason": "grounded MEMORY requires one CHECKS and one BINDINGS section",
        }]))
    checks = parse_source_checks("\n".join(sections["CHECKS"]), allowed_refs)
    binding_lines = [line for line in sections["BINDINGS"] if line.upper() != "NONE"]
    memory = normalize_memory_queries(parse_memory("\n".join(binding_lines), recover=True), query_ids)
    return GroundedMemoryResult(checks, memory)
