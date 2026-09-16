"""Resolve document labels only against explicitly supplied, visible sources."""

from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Iterable

from .fact_protocol import FactUpdate, ProtocolError, resolve_query_id


_DOCUMENT_ID = re.compile(r"context:D([1-9][0-9]*)\Z")
_FRAGMENT_ID = re.compile(r":c([0-9]+):D([1-9][0-9]*)\Z")
_DOCUMENT_LABEL = re.compile(r"(?:(?:document|doc|d)[ _-]*)?([1-9][0-9]*)(?:@c([0-9]+))?\Z", re.I)


def source_label(entry: dict[str, Any]) -> str:
    """A short label for raw numbered documents, or the existing source id."""
    match = _FRAGMENT_ID.search(entry["source_ref"])
    document = _DOCUMENT_ID.fullmatch(entry.get("document_id", ""))
    if match and document and match[2] == document[1]:
        return f"D{int(match[2])}@C{int(match[1])}"
    return entry["source_ref"]


class VisibleSources:
    def __init__(self, entries: Iterable[dict[str, Any]]):
        self.entries = {entry["source_ref"]: entry for entry in entries}
        self.by_document: dict[int, list[str]] = defaultdict(list)
        self.by_fragment: dict[tuple[int, int], list[str]] = defaultdict(list)
        for ref, entry in self.entries.items():
            document = _DOCUMENT_ID.fullmatch(entry.get("document_id", ""))
            fragment = _FRAGMENT_ID.search(ref)
            if document and fragment and document[1] == fragment[2]:
                number, window = int(document[1]), int(fragment[1])
                self.by_document[number].append(ref)
                self.by_fragment[number, window].append(ref)

    def labels(self) -> list[str]:
        return [source_label(entry) for entry in self.entries.values()]

    def resolve(self, value: str) -> list[str]:
        if value in self.entries:
            return [value]
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1].strip()
        if value in self.entries:
            return [value]
        match = _DOCUMENT_LABEL.fullmatch(value)
        if match:
            number = int(match[1])
            refs = (self.by_fragment.get((number, int(match[2])), []) if match[2] is not None
                    else self.by_document.get(number, []))
            # The same document number in different samples is not one source.
            if refs and len({self.entries[ref]["sample_id"] for ref in refs}) == 1:
                return list(refs)
        if re.fullmatch(r"F[0-9a-f]+", value, re.I):
            raise ProtocolError(f"memory fact ID {value!r} is not an original-source ID; "
                                "cite supporting visible original text, or omit the fact")
        raise ProtocolError(f"source {value!r} is not among the supplied visible sources")

    def normalize_update(self, update: FactUpdate, *, query_ids: set[str] | None = None,
                         recover: bool = False) -> tuple[FactUpdate, dict[str, list[str]]]:
        mappings: dict[str, list[str]] = {}

        def normalize(refs: list[str]) -> list[str]:
            resolved = []
            for ref in refs:
                canonical = self.resolve(ref)
                if canonical != [ref]:
                    mappings[ref] = canonical
                resolved.extend(canonical)
            return list(dict.fromkeys(resolved))

        result = update.model_copy(deep=True)
        result.facts = []
        result.hints = []
        for fact in update.facts:
            try:
                query_id = resolve_query_id(fact.query_id, query_ids) if query_ids is not None else fact.query_id
                result.facts.append(fact.model_copy(update={"query_id": query_id, "source_refs": normalize(fact.source_refs)}))
                if query_id != fact.query_id:
                    result.query_id_mappings[fact.query_id] = query_id
            except ProtocolError as exc:
                if not recover:
                    raise
                result.rejected_lines.append({"line": f"{fact.query_id} | {','.join(fact.source_refs)} | {fact.text} | {fact.relevance}", "reason": str(exc)})
        for hint in update.hints:
            try:
                result.hints.append(hint.model_copy(update={"source_refs": normalize(hint.source_refs)}))
            except ProtocolError as exc:
                if not recover:
                    raise
                result.rejected_lines.append({"line": f"PLAN_HINT | {','.join(hint.source_refs)} | {hint.text}", "reason": str(exc)})
        return result, mappings
