"""Evaluation metrics for Direct and V5 experiment result records."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from .data import CanonicalSample
from .schema import QueryPlan


_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_answer(value: Any) -> str:
    """Apply conservative 2Wiki-style normalization for answer comparison."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = "" if value is None else str(value)
    text = _PUNCTUATION.sub(" ", text.casefold())
    return " ".join(text.split())


def answer_exact(prediction: Any, gold_answers: Iterable[Any]) -> bool:
    normalized = normalize_answer(prediction)
    return bool(normalized) and any(normalized == normalize_answer(answer) for answer in gold_answers)


def answer_token_f1(prediction: Any, gold_answers: Iterable[Any]) -> float:
    pred_tokens = normalize_answer(prediction).split()
    if not pred_tokens:
        return 0.0
    best = 0.0
    for answer in gold_answers:
        gold_tokens = normalize_answer(answer).split()
        if not gold_tokens:
            continue
        common = Counter(pred_tokens) & Counter(gold_tokens)
        overlap = sum(common.values())
        if not overlap:
            continue
        precision = overlap / len(pred_tokens)
        recall = overlap / len(gold_tokens)
        best = max(best, 2 * precision * recall / (precision + recall))
    return best


def gold_source_refs(sample: CanonicalSample) -> set[str]:
    """Convert canonical supporting-fact annotations into stable source refs."""
    title_to_document = {
        document.title.casefold().strip(): document.document_id
        for document in sample.documents
    }
    refs: set[str] = set()
    for title, sentence_id in sample.supporting_facts:
        document_id = title_to_document.get(title.casefold().strip())
        if document_id is not None:
            refs.add(f"{sample.sample_id}:{document_id}:s{sentence_id}")
    return refs


def source_ref_f1(predicted: Iterable[str], gold: Iterable[str]) -> tuple[float, float, float]:
    predicted_set, gold_set = set(predicted), set(gold)
    if not predicted_set and not gold_set:
        return 1.0, 1.0, 1.0
    if not predicted_set or not gold_set:
        return 0.0, 0.0, 0.0
    overlap = len(predicted_set & gold_set)
    precision = overlap / len(predicted_set)
    recall = overlap / len(gold_set)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _normalized_triple(value: Iterable[Any]) -> tuple[str, str, str]:
    subject, relation, obj = value
    return normalize_answer(subject), normalize_answer(relation), normalize_answer(obj)


def triple_f1(
    predicted: Iterable[Iterable[Any]], gold: Iterable[Iterable[Any]]
) -> tuple[float, float, float]:
    return source_ref_f1(
        {_normalized_triple(item) for item in predicted},
        {_normalized_triple(item) for item in gold},
    )


def plan_relation_recall(predicted: QueryPlan | None, oracle: QueryPlan | None) -> float | None:
    if oracle is None:
        return None
    gold = {normalize_answer(pattern.relation_key) for pattern in oracle.patterns}
    if not gold:
        return 1.0
    if predicted is None:
        return 0.0
    observed = {normalize_answer(pattern.relation_key) for pattern in predicted.patterns}
    return len(gold & observed) / len(gold)


def _v5_predicted_answer(result: dict[str, Any]) -> Any:
    evidence_pack = result.get("evidence_pack") or {}
    return evidence_pack.get("answer_value")


def _v5_source_refs(result: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for claim in (result.get("evidence_pack") or {}).get("claims", []):
        for assertion in claim.get("evidence_assertions", []):
            source_ref = assertion.get("source_ref")
            if source_ref:
                refs.add(str(source_ref))
    return refs


def _direct_predicted_answer(result: dict[str, Any]) -> Any:
    return (result.get("answer") or {}).get("answer")


def _event_counts(result: dict[str, Any]) -> Counter[str]:
    counts = Counter(result.get("event_counts") or {})
    if counts:
        return counts
    return Counter(
        event.get("event_type")
        for event in result.get("events", [])
        if event.get("event_type")
    )


def _event_items(result: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    return [event for event in result.get("events", []) if event.get("event_type") == event_type]


def score_result(result: dict[str, Any], sample: CanonicalSample) -> dict[str, Any]:
    """Score one method result while preserving diagnostic fields."""
    method = str(result.get("method", "v5"))
    prediction = _direct_predicted_answer(result) if method.startswith("direct") else _v5_predicted_answer(result)
    gold = sample.answers or ([sample.answer] if sample.answer is not None else [])
    predicted_refs = (
        set((result.get("answer") or {}).get("source_refs", []))
        if method.startswith("direct")
        else _v5_source_refs(result)
    )
    gold_refs = gold_source_refs(sample)
    precision, recall, supporting_f1 = source_ref_f1(predicted_refs, gold_refs)
    state = result.get("state") or {}
    event_counts = _event_counts(result)
    graph_triples = [
        (claim.get("subject"), claim.get("relation"), claim.get("object"))
        for claim in (result.get("evidence_pack") or {}).get("claims", [])
    ]
    graph_precision, graph_recall, graph_triple_f1 = triple_f1(
        graph_triples, sample.evidences
    )
    proposed_triples = []
    for event in _event_items(result, "TRIPLE_EVENT_PROPOSED"):
        proposal = event.get("payload", {}).get("event", {})
        proposed_triples.append(
            (
                proposal.get("subject"),
                proposal.get("matched_family") or proposal.get("concrete_relation"),
                proposal.get("object"),
            )
        )
    event_precision, event_recall, event_triple_f1 = triple_f1(
        proposed_triples, sample.evidences
    )
    lookup_events = _event_items(result, "DEFERRED_LOOKUP")
    lookup_hits = sum(bool(event.get("payload", {}).get("claim_ids")) for event in lookup_events)
    deferred_events = event_counts.get("CLAIM_DEFERRED", 0)
    promoted_events = sum(
        event.get("payload", {}).get("origin") == "DEFERRED"
        for event in _event_items(result, "CLAIM_PROMOTED")
    )
    return {
        **result,
        "prediction": prediction,
        "gold_answers": gold,
        "answer_exact": answer_exact(prediction, gold),
        "answer_token_f1": answer_token_f1(prediction, gold),
        "predicted_source_refs": sorted(predicted_refs),
        "gold_source_refs": sorted(gold_refs),
        "supporting_precision": precision,
        "supporting_recall": recall,
        "supporting_f1": supporting_f1,
        "graph_triple_precision": graph_precision,
        "graph_triple_recall": graph_recall,
        "graph_triple_f1": graph_triple_f1,
        "triple_event_precision": event_precision,
        "triple_event_recall": event_recall,
        "triple_event_f1": event_triple_f1,
        "runtime_status": state.get("status") or result.get("status", "UNKNOWN"),
        "deferred_count": len(state.get("deferred", {})),
        "pending_count": len(state.get("pending", {})),
        "verified_count": len(state.get("verified", {})),
        "callback_count": event_counts.get("DEFERRED_LOOKUP", 0),
        "callback_hit_rate": lookup_hits / len(lookup_events) if lookup_events else 0.0,
        "promoted_count": event_counts.get("CLAIM_PROMOTED", 0),
        "deferred_promoted_count": promoted_events,
        "deferred_to_promoted": promoted_events / deferred_events if deferred_events else 0.0,
        "conflict_count": event_counts.get("VERIFY_CONFLICT", 0),
        "run_success": result.get("status") == "OK",
    }


def summarize_results(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scored rows by method/order and compute reverse-forward gap."""
    rows = list(results)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row.get("method", "unknown")), str(row.get("order", "original")))].append(row)

    def mean(items: list[dict[str, Any]], key: str) -> float | None:
        values = [item.get(key) for item in items if item.get(key) is not None]
        return sum(float(value) for value in values) / len(values) if values else None

    summaries: list[dict[str, Any]] = []
    for (method, order), items in sorted(groups.items()):
        status_counts = Counter(str(item.get("runtime_status", "UNKNOWN")) for item in items)
        summaries.append(
            {
                "method": method,
                "order": order,
                "count": len(items),
                "answer_accuracy": mean(items, "answer_exact"),
                "answer_token_f1": mean(items, "answer_token_f1"),
                "supporting_f1": mean(items, "supporting_f1"),
                "graph_triple_f1": mean(items, "graph_triple_f1"),
                "triple_event_f1": mean(items, "triple_event_f1"),
                "plan_valid": mean(items, "plan_valid"),
                "plan_relation_recall": mean(items, "plan_relation_recall"),
                "early_latent_evidence_recall": mean(items, "early_latent_evidence_recall"),
                "deferred_count": mean(items, "deferred_count"),
                "callback_count": mean(items, "callback_count"),
                "promoted_count": mean(items, "promoted_count"),
                "deferred_to_promoted": mean(items, "deferred_to_promoted"),
                "callback_hit_rate": mean(items, "callback_hit_rate"),
                "model_calls": mean(items, "model_calls"),
                "latency_ms": mean(items, "latency_ms"),
                "error_rate": 1.0 - (mean(items, "run_success") or 0.0),
                "insufficient_rate": status_counts.get("INSUFFICIENT", 0) / len(items),
                "conflicted_rate": status_counts.get("CONFLICTED", 0) / len(items),
                "unsupported_rate": status_counts.get("UNSUPPORTED", 0) / len(items),
                "resource_limit_rate": status_counts.get("RESOURCE_LIMIT", 0) / len(items),
                "runtime_status_counts": dict(sorted(status_counts.items())),
            }
        )
    gaps: dict[str, float] = {}
    for method in {summary["method"] for summary in summaries}:
        forward = next((item["answer_accuracy"] for item in summaries if item["method"] == method and item["order"] in {"original", "forward"}), None)
        reverse = next((item["answer_accuracy"] for item in summaries if item["method"] == method and item["order"] == "reverse"), None)
        if forward is not None and reverse is not None:
            gaps[method] = forward - reverse
    return {"count": len(rows), "groups": summaries, "reverse_forward_gap": gaps}
