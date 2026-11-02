"""V06b JSON adapters for MEMORY and source-grounded MEMORY responses."""

from __future__ import annotations

import json
from typing import Any

from .fact_protocol import (
    BindingProposal,
    GroundedMemoryResult,
    MemoryUpdate,
    ProtocolError,
    SourceCheck,
    SourceCheckResult,
)


MEMORY_JSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["bindings", "rebindings"],
    "properties": {
        "bindings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["query", "value", "support"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "value": {},
                "support": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
            },
        }},
        "rebindings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["query", "value", "support"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "value": {},
                "support": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
            },
        }},
    },
}

GROUNDED_MEMORY_JSON_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["checks", "bindings", "rebindings"],
    "properties": {
        "checks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["query", "fact", "verdict", "sources", "note"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "fact": {"type": "string", "minLength": 1},
                "verdict": {"type": "string", "enum": ["SUPPORTED", "SOURCE_DIFF", "UNRESOLVED"]},
                "sources": {"type": "array", "items": {"type": "string", "minLength": 1}},
                "note": {"type": "string"},
            },
        }},
        "bindings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["query", "value", "support"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "value": {},
                "support": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
            },
        }},
        "rebindings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["query", "value", "support"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "value": {},
                "support": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "minLength": 1}},
            },
        }},
    },
}


class MemoryJSONError(ProtocolError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(f"{category}: {message}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MemoryJSONError("DUPLICATE_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise MemoryJSONError("JSON_SYNTAX", f"non-JSON constant: {value}")


def _parse(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except json.JSONDecodeError as exc:
        raise MemoryJSONError("JSON_SYNTAX", f"{exc.msg} at character {exc.pos}") from exc
    if not isinstance(value, dict):
        raise MemoryJSONError("ENVELOPE", "response must be one JSON object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _proposal(item: Any, *, collection: str, index: int, query_ids: set[str] | None) -> BindingProposal:
    if not isinstance(item, dict) or set(item) != {"query", "value", "support"}:
        raise ValueError("expected exactly query, value and support")
    query = _string(item["query"], "query")
    if query_ids is not None and query not in query_ids:
        raise ValueError(f"unknown query ID {query}")
    support = item["support"]
    if not isinstance(support, list) or not support or any(not isinstance(ref, str) or not ref.strip() for ref in support):
        raise ValueError("support must be a nonempty array of fact IDs")
    if any(ref.strip().upper() in {"NONE", "NULL"} for ref in support):
        raise ValueError("support cannot contain NONE or NULL")
    return BindingProposal(query_id=query, value=item["value"], support_refs=list(dict.fromkeys(support)))


def parse_memory_json(raw: str, *, query_ids: set[str] | None = None) -> MemoryUpdate:
    """Parse ordinary MEMORY JSON and retain independent valid proposals."""
    payload = _parse(raw)
    if set(payload) != {"bindings", "rebindings"}:
        raise MemoryJSONError("ENVELOPE", "expected exactly bindings and rebindings arrays")
    if not all(isinstance(payload[key], list) for key in ("bindings", "rebindings")):
        raise MemoryJSONError("ENVELOPE", "bindings and rebindings must be arrays")
    result = MemoryUpdate()
    for collection, target in (("bindings", result.bindings), ("rebindings", result.rebindings)):
        for index, item in enumerate(payload[collection]):
            try:
                target.append(_proposal(item, collection=collection, index=index, query_ids=query_ids))
            except (ValueError, TypeError) as exc:
                result.rejected_lines.append({
                    "line": json.dumps({collection: [item]}, ensure_ascii=False),
                    "reason": f"{collection}[{index}]: {exc}",
                })
    return result


def _check_result(payload: Any, allowed_refs: dict[tuple[str, str], set[str]],
                  fact_aliases: dict[tuple[str, str], str] | None = None) -> SourceCheckResult:
    checks: dict[tuple[str, str], SourceCheck] = {}
    rejected: list[dict[str, str]] = []
    conflicted: set[tuple[str, str]] = set()
    for index, item in enumerate(payload):
        try:
            if not isinstance(item, dict) or set(item) != {"query", "fact", "verdict", "sources", "note"}:
                raise ValueError("expected exactly query, fact, verdict, sources and note")
            query = _string(item["query"], "query")
            fact = _string(item["fact"], "fact")
            key = (query, fact)
            # Qwen may copy the displayed fact sentence instead of its ID.
            # Resolve only an exact sentence from this call's evidence table;
            # never infer an ID from an arbitrary model string.
            if key not in allowed_refs and fact_aliases is not None:
                fact_id = fact_aliases.get(key)
                if fact_id is not None:
                    key = (query, fact_id)
            if key not in allowed_refs:
                raise ValueError("unknown query/fact pair")
            verdict = _string(item["verdict"], "verdict").upper()
            if verdict not in {"SUPPORTED", "SOURCE_DIFF", "UNRESOLVED"}:
                raise ValueError("unknown verdict")
            refs = item["sources"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
                raise ValueError("sources must be an array of strings")
            refs = list(dict.fromkeys(refs))
            if verdict != "UNRESOLVED" and not refs:
                raise ValueError("supported checks require source refs")
            if not set(refs) <= allowed_refs[key]:
                raise ValueError("source ref outside allowed scope")
            note = item["note"]
            if not isinstance(note, str):
                raise ValueError("note must be a string")
            check = SourceCheck(query_id=key[0], fact_id=key[1], verdict=verdict,
                                source_refs=refs, note=note)
            prior = checks.get(key)
            if prior and (prior.verdict != check.verdict or prior.source_refs != check.source_refs):
                raise ValueError("conflicting duplicate check")
            checks[key] = check
        except (ValueError, TypeError) as exc:
            rejected.append({"line": json.dumps({"checks": [item]}, ensure_ascii=False),
                             "reason": f"checks[{index}]: {exc}"})
            if isinstance(item, dict):
                key = (str(item.get("query", "")), str(item.get("fact", "")))
                if key in allowed_refs:
                    conflicted.add(key)
    for key in allowed_refs:
        if key not in checks or key in conflicted:
            checks[key] = SourceCheck(query_id=key[0], fact_id=key[1], verdict="UNRESOLVED")
    return SourceCheckResult(checks=tuple(checks[key] for key in sorted(checks)),
                             rejected_lines=tuple(rejected))


def parse_grounded_memory_json(raw: str, *, allowed_refs: dict[tuple[str, str], set[str]],
                               query_ids: set[str],
                               fact_aliases: dict[tuple[str, str], str] | None = None) -> GroundedMemoryResult:
    payload = _parse(raw)
    if set(payload) != {"checks", "bindings", "rebindings"}:
        raise MemoryJSONError("ENVELOPE", "expected exactly checks, bindings and rebindings arrays")
    if not all(isinstance(payload[key], list) for key in ("checks", "bindings", "rebindings")):
        raise MemoryJSONError("ENVELOPE", "checks, bindings and rebindings must be arrays")
    checks = _check_result(payload["checks"], allowed_refs, fact_aliases=fact_aliases)
    memory = parse_memory_json(json.dumps({"bindings": payload["bindings"],
                                           "rebindings": payload["rebindings"]}, ensure_ascii=False),
                               query_ids=query_ids)
    return GroundedMemoryResult(
        checks=SourceCheckResult(checks=checks.checks,
                                 rejected_lines=checks.rejected_lines),
        memory=memory,
    )


def memory_response_format(mode: str) -> dict[str, Any] | None:
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {
            "name": "memory", "strict": True, "schema": MEMORY_JSON_SCHEMA,
        }}
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "prompt":
        return None
    raise ValueError(f"unknown MEMORY JSON transport mode: {mode}")


def grounded_memory_response_format(mode: str) -> dict[str, Any] | None:
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {
            "name": "memory_grounded", "strict": True, "schema": GROUNDED_MEMORY_JSON_SCHEMA,
        }}
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "prompt":
        return None
    raise ValueError(f"unknown MEMORY JSON transport mode: {mode}")
