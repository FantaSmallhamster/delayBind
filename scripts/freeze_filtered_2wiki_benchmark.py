"""Freeze the retained 2Wiki no-context subset as a 50-document benchmark."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import random

from build_2wiki_benchmark import build_records, load_source, prepare_source


DEFAULT_QUOTAS = {
    "compositional": 53,
    "inference": 16,
    "comparison": 35,
    "bridge_comparison": 24,
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def question_key(text: str) -> str:
    return " ".join(text.strip().rstrip("?").casefold().split())


def frozen_write(path: Path, payload: bytes) -> None:
    if path.exists() and path.read_bytes() != payload:
        raise ValueError(f"frozen output differs: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-source", type=Path, required=True)
    parser.add_argument("--flashrag-source", type=Path, required=True)
    parser.add_argument("--retained", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=4)
    parser.add_argument("--num-docs", type=int, default=50)
    args = parser.parse_args()

    official = json.loads(args.official_source.read_text())
    official_position = {row["_id"]: i for i, row in enumerate(official)}
    if len(official_position) != len(official):
        raise ValueError("official source contains duplicate IDs")

    retained_by_id: dict[str, dict] = {}
    source_batch: dict[str, str] = {}
    for path in args.retained:
        for row in json.loads(path.read_text()):
            sid = row["_id"]
            if sid in retained_by_id:
                raise ValueError(f"retained ID appears in multiple batches: {sid}")
            retained_by_id[sid] = row
            source_batch[sid] = path.parent.name

    pools: dict[str, list[dict]] = {}
    for kind in DEFAULT_QUOTAS:
        pools[kind] = sorted(
            (row for row in retained_by_id.values() if row["type"] == kind),
            key=lambda row: official_position[row["_id"]],
        )
        if len(pools[kind]) < DEFAULT_QUOTAS[kind]:
            raise ValueError(f"insufficient retained {kind} records")

    rng = random.Random(args.seed)
    selected: list[dict] = []
    for kind, quota in DEFAULT_QUOTAS.items():
        selected.extend(rng.sample(pools[kind], quota))
    if len(selected) != 128 or len({row["_id"] for row in selected}) != 128:
        raise ValueError("selection must contain 128 unique records")

    flash = load_source(args.flashrag_source)
    questions, corpus = prepare_source(flash)
    flash_by_key = {question_key(row["question"]): row for row in flash}
    question_by_key = {question_key(row["question"]): row for row in questions}
    if len(flash_by_key) != len(flash) or len(question_by_key) != len(questions):
        raise ValueError("FlashRAG source contains duplicate normalized questions")
    selected_questions = [question_by_key[question_key(row["question"])] for row in selected]
    benchmark = build_records(
        selected_questions,
        corpus,
        seed=args.seed,
        sample_count=128,
        num_docs=args.num_docs,
        document_permutation_seed=4,
    )

    selected_by_key = {question_key(row["question"]): row for row in selected}
    ordered_official = [selected_by_key[question_key(row["input"])] for row in benchmark]
    ordered_flash = [flash_by_key[question_key(row["input"])] for row in benchmark]
    for index, (bench, source_row) in enumerate(zip(benchmark, ordered_official)):
        bench["frozen_index"] = index
        bench["official_id"] = source_row["_id"]
        bench["filter_batch"] = source_batch[source_row["_id"]]

    output_dir = args.output_dir
    outputs = {
        "selected_official.json": json_bytes(ordered_official),
        "selected_source.jsonl": ("\n".join(json.dumps(row, ensure_ascii=False) for row in ordered_flash) + "\n").encode(),
        "eval_2wikimultihopqa_50.json": json_bytes(benchmark),
    }
    for name, payload in outputs.items():
        frozen_write(output_dir / name, payload)

    csv_buffer = io.StringIO()
    writer = csv.DictWriter(
        csv_buffer,
        fieldnames=[
            "frozen_index", "official_id", "benchmark_id", "type", "question", "answer", "filter_batch"
        ],
        lineterminator="\n",
    )
    writer.writeheader()
    for index, (bench, source_row) in enumerate(zip(benchmark, ordered_official)):
        writer.writerow({
            "frozen_index": index,
            "official_id": source_row["_id"],
            "benchmark_id": bench["id"],
            "type": source_row["type"],
            "question": source_row["question"],
            "answer": source_row["answer"],
            "filter_batch": source_batch[source_row["_id"]],
        })
    frozen_write(output_dir / "questions.csv", b"\xef\xbb\xbf" + csv_buffer.getvalue().encode())

    manifest = {
        "frozen": True,
        "selection_seed": args.seed,
        "selection_rng_schedule": "one Random(seed), sampled serially in quota key order",
        "quota_order": list(DEFAULT_QUOTAS),
        "requested_original_proportions": {
            "compositional": 53, "inference": 16, "comparison": 31, "bridge_comparison": 28,
        },
        "final_quotas": DEFAULT_QUOTAS,
        "quota_adjustment": "four unavailable bridge_comparison slots reassigned to comparison",
        "retained_pool_counts": {kind: len(pool) for kind, pool in pools.items()},
        "sample_count": len(benchmark),
        "num_docs": args.num_docs,
        "type_counts": dict(Counter(row["type"] for row in ordered_official)),
        "official_source": str(args.official_source),
        "official_source_sha256": digest(args.official_source),
        "flashrag_source": str(args.flashrag_source),
        "flashrag_source_sha256": digest(args.flashrag_source),
        "retained_sources": [
            {"path": str(path), "sha256": digest(path), "count": len(json.loads(path.read_text()))}
            for path in args.retained
        ],
        "outputs": {name: {"sha256": hashlib.sha256(payload).hexdigest()} for name, payload in outputs.items()},
        "questions_csv_sha256": digest(output_dir / "questions.csv"),
        "official_ids": [row["_id"] for row in ordered_official],
        "benchmark_ids": [row["id"] for row in benchmark],
        "generator": str(Path(__file__)),
        "generator_sha256": digest(Path(__file__)),
    }
    frozen_write(output_dir / "frozen_manifest.json", json_bytes(manifest))
    print(json.dumps({
        "output_dir": str(output_dir),
        "sample_count": len(benchmark),
        "type_counts": manifest["type_counts"],
        "benchmark_sha256": manifest["outputs"]["eval_2wikimultihopqa_50.json"]["sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
