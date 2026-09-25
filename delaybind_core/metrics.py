"""Evaluation metrics for R2 experiment result records."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from .data import CanonicalSample


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


def score_result(result: dict[str, Any], sample: CanonicalSample) -> dict[str, Any]:
    """Score an R2 result while preserving diagnostic fields."""
    answer = result.get("answer") or {}
    prediction = answer.get("answer")
    gold = sample.answers or ([sample.answer] if sample.answer is not None else [])
    mapping = result.get("source_ref_map", {})
    refs = {mapping[r] for r in answer.get("source_refs", []) if r in mapping}
    precision, recall, f1 = source_ref_f1(refs, gold_source_refs(sample))
    return {**result, "prediction": prediction, "gold_answers": gold,
            "answer_exact": answer_exact(prediction, gold), "answer_token_f1": answer_token_f1(prediction, gold),
            "abstained": prediction is None, "predicted_source_refs": sorted(refs),
            "gold_source_refs": sorted(gold_source_refs(sample)),
            "supporting_precision": precision if sample.supporting_facts else None,
            "supporting_recall": recall if sample.supporting_facts else None,
            "supporting_f1": f1 if sample.supporting_facts else None,
            "runtime_status": result.get("state", {}).get("status", result.get("status")),
            "run_success": result.get("state", {}).get("status") in {"ANSWERED", "INSUFFICIENT"},
            "windows_processed": result.get("windows"), **result.get("v52_metrics", {}), **result.get("r2_metrics", {}),
            "verifier_precision": None, "verifier_recall": None, "verifier_f1": None,
            "graph_triple_f1": None, "triple_event_f1": None}


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
        verifier_items = [item for item in items if item.get("verifier_tp") is not None]
        verifier_tp = sum(int(item.get("verifier_tp") or 0) for item in verifier_items)
        verifier_fp = sum(int(item.get("verifier_fp") or 0) for item in verifier_items)
        verifier_fn = sum(int(item.get("verifier_fn") or 0) for item in verifier_items)
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
                "verifier_tp": verifier_tp if verifier_items else None,
                "verifier_fp": verifier_fp if verifier_items else None,
                "verifier_fn": verifier_fn if verifier_items else None,
                "verifier_candidate_count": mean(items, "verifier_candidate_count"),
                "verifier_gold_candidate_count": mean(items, "verifier_gold_candidate_count"),
                "windows_processed": mean(items, "windows_processed"),
                "streaming_protocol_valid_rate": mean(items, "streaming_protocol_valid"),
                "plan_valid": mean(items, "plan_valid"),
                "protocol_valid_rate": mean(items, "protocol_valid"),
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
                **{key: mean(items, key) for key in (
                    "anchor_valid_rate", "raw_coverage_of_accepted_facts", "raw_coverage_of_binding_support",
                    "recall_scan_completion", "source_review_acceptance", "dual_graph_consistency",
                    "stale_binding_rate", "transaction_replay_consistency")},
            }
        )
    gaps: dict[str, float] = {}
    for method in {summary["method"] for summary in summaries}:
        forward = next((item["answer_accuracy"] for item in summaries if item["method"] == method and item["order"] in {"original", "forward"}), None)
        reverse = next((item["answer_accuracy"] for item in summaries if item["method"] == method and item["order"] == "reverse"), None)
        if forward is not None and reverse is not None:
            gaps[method] = forward - reverse
    return {"count": len(rows), "groups": summaries, "reverse_forward_gap": gaps}
