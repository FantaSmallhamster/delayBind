#!/usr/bin/env python3
"""Score predictions against the separate gold file in this bundle."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


BUNDLE_ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = BUNDLE_ROOT / "grading" / "gold_answers.jsonl"
PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_answer(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = "" if value is None else str(value)
    normalized = " ".join(PUNCTUATION.sub(" ", text.casefold()).split())
    if normalized == "true":
        return "yes"
    if normalized == "false":
        return "no"
    return normalized


def exact_match(prediction: Any, answers: list[Any]) -> bool:
    normalized = normalize_answer(prediction)
    return bool(normalized) and any(normalized == normalize_answer(answer) for answer in answers)


def token_f1(prediction: Any, answers: list[Any]) -> float:
    predicted = normalize_answer(prediction).split()
    if not predicted:
        return 0.0
    pred_counts = Counter(predicted)
    best = 0.0
    for answer in answers:
        gold = normalize_answer(answer).split()
        if not gold:
            continue
        overlap = sum((pred_counts & Counter(gold)).values())
        if overlap:
            precision = overlap / len(predicted)
            recall = overlap / len(gold)
            best = max(best, 2 * precision * recall / (precision + recall))
    return best


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected a JSON object at {path}:{line_number}")
            rows.append(value)
    return rows


def prediction_value(row: dict[str, Any]) -> Any:
    if "prediction" in row:
        return row["prediction"]
    answer = row.get("answer")
    if isinstance(answer, dict):
        return answer.get("answer")
    return answer


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predictions", type=Path, help="JSONL with sample_id/id and prediction/answer")
    parser.add_argument("--scored-output", type=Path,
                        help="Optional path for per-question scores as JSONL")
    args = parser.parse_args()

    if not args.predictions.is_file():
        parser.error(f"prediction file does not exist: {args.predictions}")
    if not GOLD_PATH.is_file():
        parser.error(f"gold file is missing from bundle: {GOLD_PATH}")

    gold_rows = read_jsonl(GOLD_PATH)
    gold_by_id = {str(row["sample_id"]): row for row in gold_rows}
    if len(gold_by_id) != len(gold_rows):
        raise SystemExit("Gold file contains duplicate sample IDs")

    predictions: dict[str, Any] = {}
    for row in read_jsonl(args.predictions):
        sample_id = row.get("sample_id", row.get("id"))
        if sample_id is None:
            raise SystemExit("Each prediction row must contain sample_id or id")
        sample_id = str(sample_id)
        if sample_id not in gold_by_id:
            raise SystemExit(f"Unknown sample_id in predictions: {sample_id}")
        if sample_id in predictions:
            raise SystemExit(f"Duplicate prediction for sample_id: {sample_id}")
        predictions[sample_id] = prediction_value(row)

    scored = []
    for sample_id, gold in gold_by_id.items():
        prediction = predictions.get(sample_id)
        answers = gold["gold_answers"]
        scored.append({
            "sample_id": sample_id,
            "prediction": prediction,
            "gold_answers": answers,
            "submitted": sample_id in predictions,
            "answer_exact": exact_match(prediction, answers),
            "answer_token_f1": token_f1(prediction, answers),
        })

    count = len(scored)
    correct = sum(row["answer_exact"] for row in scored)
    mean_f1 = sum(row["answer_token_f1"] for row in scored) / count if count else 0.0
    submitted = len(predictions)
    summary = {
        "count": count,
        "submitted": submitted,
        "missing": count - submitted,
        "correct": correct,
        "accuracy_em": correct / count if count else 0.0,
        "mean_token_f1": mean_f1,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.scored_output:
        args.scored_output.parent.mkdir(parents=True, exist_ok=True)
        with args.scored_output.open("w", encoding="utf-8") as stream:
            for row in scored:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    missing_ids = [row["sample_id"] for row in scored if not row["submitted"]]
    if missing_ids:
        print("Missing sample IDs (first 20): " + ", ".join(missing_ids[:20]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
