"""V06a: JSON serialization for state-free UPDATE facts and plan hints."""

import json
from typing import Any

from .fact_protocol import FactEvent, FactUpdate, PlanHint, ProtocolError
from .source_refs import VisibleSources
from .fact_protocol import resolve_query_id


UPDATE_JSON_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts", "hints"],
    "properties": {
        "facts": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["query", "sources", "text"],
                "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "sources": {"type": "array", "minItems": 1,
                                "items": {"type": "string", "minLength": 1}},
                    "text": {"type": "string", "minLength": 1},
                },
            },
        },
        "hints": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["sources", "text"],
                "properties": {
                    "sources": {"type": "array", "minItems": 1,
                                "items": {"type": "string", "minLength": 1}},
                    "text": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class UpdateJSONError(ProtocolError):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(f"{category}: {message}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateJSONError("DUPLICATE_KEY", f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise UpdateJSONError("JSON_SYNTAX", f"non-JSON constant: {value}")


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def parse_update_json(raw: str, *, sources: VisibleSources | None = None,
                      query_ids: set[str] | None = None) -> FactUpdate:
    """Reject ambiguous envelopes; retain independent valid items with diagnostics.

    There is no text-protocol fallback, state field, type coercion, or extraction
    of fragments from truncated JSON. Source/ID checks match the text adapter.
    """
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except json.JSONDecodeError as exc:
        raise UpdateJSONError("JSON_SYNTAX", f"{exc.msg} at character {exc.pos}") from exc
    if not isinstance(payload, dict) or set(payload) != {"facts", "hints"}:
        raise UpdateJSONError("ENVELOPE", "expected exactly facts and hints")
    if not isinstance(payload["facts"], list) or not isinstance(payload["hints"], list):
        raise UpdateJSONError("ENVELOPE", "facts and hints must be arrays")
    result = FactUpdate()
    for collection in ("facts", "hints"):
        expected = {"sources", "text"} | ({"query"} if collection == "facts" else set())
        for index, item in enumerate(payload[collection]):
            try:
                if not isinstance(item, dict) or set(item) != expected:
                    raise ValueError(f"expected exactly {sorted(expected)}")
                text = _nonempty_string(item["text"], "text")
                refs = item["sources"]
                if not isinstance(refs, list) or not refs:
                    raise ValueError("sources must be a nonempty array of strings")
                for ref in refs:
                    _nonempty_string(ref, "source")
                    if ref.strip().upper() in {"NONE", "NULL"}:
                        raise ValueError("sources cannot contain NONE or NULL")
                if sources is not None:
                    refs = list(dict.fromkeys(ref for label in refs for ref in sources.resolve(label)))
                if collection == "facts":
                    qid = _nonempty_string(item["query"], "query")
                    if query_ids is not None:
                        normalized = resolve_query_id(qid, query_ids)
                        if normalized != qid:
                            result.query_id_mappings[qid] = normalized
                        qid = normalized
                    result.facts.append(FactEvent(query_id=qid, source_refs=refs, text=text))
                else:
                    result.hints.append(PlanHint(source_refs=refs, text=text))
            except (ValueError, KeyError) as exc:
                result.rejected_lines.append({
                    "line": json.dumps({collection: [item]}, ensure_ascii=False),
                    "reason": f"{collection}[{index}]: {exc}",
                })
    return result


def update_response_format(mode: str) -> dict[str, Any] | None:
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {
            "name": "update", "strict": True, "schema": UPDATE_JSON_SCHEMA,
        }}
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "prompt":
        return None
    raise ValueError(f"unknown UPDATE JSON transport mode: {mode}")
