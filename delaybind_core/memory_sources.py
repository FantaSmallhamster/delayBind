"""Read-only, query-scoped source evidence prepared before MEMORY calls."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Callable

from .archive import FutureSourceAccessError, RawArchive


@dataclass(frozen=True)
class QueryEvidenceScope:
    query_id: str
    snapshot_key: str
    allowed_action: str
    allowed_fact_ids: tuple[str, ...]
    dependency_support_ids: tuple[str, ...]


@dataclass(frozen=True)
class PreparedEvidence:
    fingerprint: str
    memory_text: str
    query_scopes: dict[str, QueryEvidenceScope]
    raw_context: str
    raw_rows: tuple[dict[str, Any], ...]
    fact_context_refs: dict[tuple[str, str], tuple[str, ...]]
    missing_targets: tuple[str, ...]

    @property
    def has_eligible_query(self) -> bool:
        return bool(self.query_scopes)

    def scope_for(self, query_id: str) -> QueryEvidenceScope | None:
        return self.query_scopes.get(query_id)

    def context_for(self, query_id: str, fact_id: str) -> tuple[str, ...]:
        return self.fact_context_refs.get((query_id, fact_id), ())

    def contract(self) -> dict[str, dict[str, Any]]:
        return {
            query_id: {
                "action": scope.allowed_action,
                "allowed_fact_ids": list(scope.allowed_fact_ids),
                "dependency_support_ids": list(scope.dependency_support_ids),
                "snapshot_key": scope.snapshot_key,
            }
            for query_id, scope in sorted(self.query_scopes.items())
        }


def _snapshot(runtime: Any, query_id: str, *, fact_ids: tuple[str, ...], eof: bool) -> str:
    execution = runtime.state.executions[query_id]
    deps = {
        dependency: runtime.state.executions[dependency].model_dump(mode="json")
        for dependency in sorted(runtime.dependency_ids(query_id))
    }
    payload = {
        "query_id": query_id,
        "execution": execution.model_dump(mode="json"),
        "dependencies": deps,
        "fact_ids": fact_ids,
        "eof": eof,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scoped_fact_ids(runtime: Any, query_id: str) -> tuple[str, ...]:
    """Return visible accepted facts legal for one query's MEMORY call."""
    visible = {fact["fact_id"] for fact in runtime.state.memory(visible_only=True)["facts"]}
    local = runtime.state.accepted_ids(query_id)
    upstream = {
        fact_id for dependency in runtime.dependency_ids(query_id)
        for fact_id in runtime.state.executions[dependency].support_fact_ids
    }
    return tuple(sorted((local | upstream) & visible))


def scope_is_current(runtime: Any, scope: QueryEvidenceScope, *, eof: bool) -> bool:
    """Check that a pre-MEMORY scope still represents the live query state."""
    current = scoped_fact_ids(runtime, scope.query_id)
    return _snapshot(runtime, scope.query_id, fact_ids=current, eof=eof) == scope.snapshot_key


def prepare_memory_evidence(
    runtime: Any,
    archive: RawArchive,
    memory_text: str,
    *,
    eof: bool,
    source_budget: int,
    measure: Callable[[str], int],
    mode: str = "raw_before_memory",
) -> PreparedEvidence:
    """Build one immutable, source-reference-limited MEMORY evidence package.

    The function does not mutate runtime state, search the archive, or admit
    candidates. It includes only facts already visible in the memory view and
    facts legal for the query or its resolved dependencies.
    """
    candidates: dict[str, tuple[str, tuple[str, ...], tuple[str, ...]]] = {}
    fact_to_queries: dict[str, set[str]] = {}
    missing: set[str] = set()
    for query in runtime.state.plan.queries:
        execution = runtime.state.executions[query.id]
        if execution.review_pending or execution.conflicts:
            continue
        if execution.status == "ACTIVE":
            action = "BIND"
        elif execution.status == "RESOLVED":
            action = "REBIND"
        else:
            continue
        dependency_support = {
            fact_id for dependency in runtime.dependency_ids(query.id)
            for fact_id in runtime.state.executions[dependency].support_fact_ids
        }
        allowed = scoped_fact_ids(runtime, query.id)
        # A query may be reasoned from only its resolved upstream evidence.
        if not allowed:
            continue
        candidates[query.id] = (action, allowed, tuple(sorted(dependency_support & set(allowed))))
        for fact_id in allowed:
            fact_to_queries.setdefault(fact_id, set()).add(query.id)

    targets: dict[str, set[str]] = {}
    for fact_id in fact_to_queries:
        fact = runtime.state.facts[fact_id]
        if not fact.source_refs:
            missing.add(f"{fact_id}:NO_SOURCE_REFS")
            continue
        for ref in fact.source_refs:
            targets.setdefault(ref, set()).add(fact_id)

    rows: dict[str, dict[str, Any]] = {}
    context_refs: dict[str, tuple[str, ...]] = {}
    for target in sorted(targets):
        try:
            entries = archive.fetch_sentence_context(target, neighborhood=1, same_document=True)
        except (FutureSourceAccessError, KeyError, ValueError):
            missing.add(target)
            continue
        if not any(entry.source_ref == target for entry in entries):
            missing.add(target)
            continue
        context_refs[target] = tuple(entry.source_ref for entry in entries)
        for entry in entries:
            rows[entry.source_ref] = {
                "source_ref": entry.source_ref,
                "title": entry.title,
                "sentence_id": entry.sentence_id,
                "stream_position": entry.stream_position,
                "text": entry.text,
                "target_for_fact_ids": sorted(targets.get(entry.source_ref, set())),
            }

    ordered = tuple(sorted(rows.values(), key=lambda row: (row["stream_position"], row["source_ref"])))
    raw_lines = []
    for row in ordered:
        target_fact_ids = row["target_for_fact_ids"]
        role = "TARGET" if target_fact_ids else "NEIGHBOR"
        raw_lines.append(
            f"[{role}] {row['source_ref']} | {row['title']} | sentence={row['sentence_id']} | {row['text']}"
        )
    raw_context = "\n".join(raw_lines)
    if measure(raw_context) > source_budget:
        raise ValueError(f"SOURCE_CONTEXT_BUDGET:{measure(raw_context)}>{source_budget}")

    valid_fact_ids = {
        fact_id for fact_id in fact_to_queries
        if runtime.state.facts[fact_id].source_refs
        and all(ref in context_refs for ref in runtime.state.facts[fact_id].source_refs)
    }
    scopes: dict[str, QueryEvidenceScope] = {}
    fact_context_refs: dict[tuple[str, str], tuple[str, ...]] = {}
    for query_id, (action, allowed, dependency_support) in candidates.items():
        eligible = tuple(fact_id for fact_id in allowed if fact_id in valid_fact_ids)
        if not eligible:
            continue
        scopes[query_id] = QueryEvidenceScope(
            query_id=query_id,
            snapshot_key=_snapshot(runtime, query_id, fact_ids=allowed, eof=eof),
            allowed_action=action,
            allowed_fact_ids=eligible,
            dependency_support_ids=tuple(fact_id for fact_id in dependency_support if fact_id in eligible),
        )
        for fact_id in eligible:
            fact_context_refs[(query_id, fact_id)] = tuple(sorted({
                ref for target in runtime.state.facts[fact_id].source_refs
                for ref in context_refs[target]
            }))

    signature_payload = {
        "memory": memory_text,
        "scopes": {query_id: scope.__dict__ for query_id, scope in sorted(scopes.items())},
        "contexts": context_refs,
        "raw_rows": [(row["source_ref"], hashlib.sha256(row["text"].encode()).hexdigest()) for row in ordered],
        "missing": sorted(missing),
        "hints": runtime.state.hints,
        "mode": mode,
    }
    fingerprint = hashlib.sha256(json.dumps(signature_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return PreparedEvidence(
        fingerprint=fingerprint,
        memory_text=memory_text,
        query_scopes=scopes,
        raw_context=raw_context,
        raw_rows=ordered,
        fact_context_refs=fact_context_refs,
        missing_targets=tuple(sorted(missing)),
    )
