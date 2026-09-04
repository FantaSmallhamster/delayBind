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
    normalized = " ".join(text.split())
    # JSON mode often serializes Boolean answers as strings.  The benchmark
    # convention uses yes/no, so preserve the semantic value before EM/F1.
    if normalized == "true":
        return "yes"
    if normalized == "false":
        return "no"
    return normalized


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


def _claim_triple(claim: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        claim.get("subject"),
        claim.get("relation"),
        claim.get("object"),
    )


def verifier_metrics(
    events: Iterable[dict[str, Any]],
    gold_evidences: Iterable[Iterable[Any]],
) -> dict[str, int | float | None]:
    """Score the terminal VERIFY decision for each submitted claim.

    This is deliberately conditional on claims that reached VERIFY.  It
    measures verifier discrimination, not UPDATE coverage; the latter remains
    ``triple_event_*``.  A NEED_MORE_CONTEXT followed by an expanded-context
    ACCEPT is counted only as ACCEPT.
    """
    terminal: dict[str, tuple[str, tuple[Any, Any, Any]]] = {}
    event_to_status = {
        "VERIFY_NEED_MORE_CONTEXT": "NEED_MORE_CONTEXT",
        "VERIFY_REJECTED": "REJECT",
        "VERIFY_CONFLICT": "CONFLICT",
        "CLAIM_PROMOTED": "ACCEPT",
    }
    for event in events:
        status = event_to_status.get(str(event.get("event_type")))
        if status is None:
            continue
        claim = event.get("payload", {}).get("claim")
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str):
            continue
        terminal[claim_id] = (status, _claim_triple(claim))
    if not terminal:
        return {
            "verifier_candidate_count": 0,
            "verifier_gold_candidate_count": 0,
            "verifier_accept_count": 0,
            "verifier_tp": 0,
            "verifier_fp": 0,
            "verifier_fn": 0,
            "verifier_precision": None,
            "verifier_recall": None,
            "verifier_f1": None,
        }

    gold = {_normalized_triple(item[:3]) for item in gold_evidences if len(item) >= 3}
    accepted = 0
    true_positive = 0
    false_positive = 0
    false_negative = 0
    gold_candidates = 0
    for status, triple in terminal.values():
        is_gold = _normalized_triple(triple) in gold
        if is_gold:
            gold_candidates += 1
        if status == "ACCEPT":
            accepted += 1
            if is_gold:
                true_positive += 1
            else:
                false_positive += 1
        elif is_gold:
            false_negative += 1
    precision = true_positive / accepted if accepted else None
    recall = true_positive / gold_candidates if gold_candidates else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    return {
        "verifier_candidate_count": len(terminal),
        "verifier_gold_candidate_count": gold_candidates,
        "verifier_accept_count": accepted,
        "verifier_tp": true_positive,
        "verifier_fp": false_positive,
        "verifier_fn": false_negative,
        "verifier_precision": precision,
        "verifier_recall": recall,
        "verifier_f1": f1,
    }


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
    cross_window_promoted = event_counts.get("CROSS_WINDOW_DEFERRED_PROMOTED", 0)
    non_early_promoted = event_counts.get("NON_EARLY_DEFERRED_PROMOTED", 0)
    verifier = verifier_metrics(result.get("events", []), sample.evidences)
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
        "cross_window_deferred_promoted_count": cross_window_promoted,
        "non_early_deferred_promoted_count": non_early_promoted,
        "cross_window_deferred_to_promoted": (
            cross_window_promoted / deferred_events if deferred_events else 0.0
        ),
        "conflict_count": event_counts.get("VERIFY_CONFLICT", 0),
        "windows_processed": result.get("windows_processed"),
        "streaming_protocol_valid": result.get("streaming_protocol_valid"),
        **verifier,
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
        verifier_tp = sum(int(item.get("verifier_tp", 0)) for item in items)
        verifier_fp = sum(int(item.get("verifier_fp", 0)) for item in items)
        verifier_fn = sum(int(item.get("verifier_fn", 0)) for item in items)
        verifier_micro_precision = (
            verifier_tp / (verifier_tp + verifier_fp)
            if verifier_tp + verifier_fp
            else None
        )
        verifier_micro_recall = (
            verifier_tp / (verifier_tp + verifier_fn)
            if verifier_tp + verifier_fn
            else None
        )
        verifier_micro_f1 = (
            2 * verifier_micro_precision * verifier_micro_recall
            / (verifier_micro_precision + verifier_micro_recall)
            if verifier_micro_precision is not None
            and verifier_micro_recall is not None
            and verifier_micro_precision + verifier_micro_recall
            else None
        )
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
                "verifier_precision": mean(items, "verifier_precision"),
                "verifier_recall": mean(items, "verifier_recall"),
                "verifier_f1": mean(items, "verifier_f1"),
                "verifier_micro_precision": verifier_micro_precision,
                "verifier_micro_recall": verifier_micro_recall,
                "verifier_micro_f1": verifier_micro_f1,
                "verifier_tp": verifier_tp,
                "verifier_fp": verifier_fp,
                "verifier_fn": verifier_fn,
                "verifier_candidate_count": mean(items, "verifier_candidate_count"),
                "verifier_gold_candidate_count": mean(items, "verifier_gold_candidate_count"),
                "windows_processed": mean(items, "windows_processed"),
                "streaming_protocol_valid_rate": mean(items, "streaming_protocol_valid"),
                "plan_valid": mean(items, "plan_valid"),
                "plan_relation_recall": mean(items, "plan_relation_recall"),
                "early_latent_evidence_recall": mean(items, "early_latent_evidence_recall"),
                "deferred_count": mean(items, "deferred_count"),
                "callback_count": mean(items, "callback_count"),
                "promoted_count": mean(items, "promoted_count"),
                "deferred_to_promoted": mean(items, "deferred_to_promoted"),
                "cross_window_deferred_promoted_count": mean(
                    items, "cross_window_deferred_promoted_count"
                ),
                "cross_window_deferred_to_promoted": mean(
                    items, "cross_window_deferred_to_promoted"
                ),
                "non_early_deferred_promoted_count": mean(
                    items, "non_early_deferred_promoted_count"
                ),
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
