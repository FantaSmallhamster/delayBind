"""Freeze a seed-controlled ReMemR1-format 2Wiki benchmark from local FlashRAG JSONL.

The released question/corpus conversion, 5*n question draw and seed-4 document
permutation are preserved. Distractors use the seeded RNG serially, avoiding the
unseeded process-worker RNGs in the released generator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
from typing import Any


def load_source(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def prepare_source(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    def documents(record):
        context = record["metadata"]["context"]
        sentences = context.get("sentences", context.get("content"))
        return [f"{title}\n{''.join(parts)}" for title, parts in zip(context["title"], sentences)]

    corpus = sorted({text for record in records for text in documents(record)})
    lookup = {text: index for index, text in enumerate(corpus)}
    questions = []
    for record in records:
        metadata = record["metadata"]
        titles = metadata["context"]["title"]
        supporting = metadata["supporting_facts"]["title"]
        if not all(title in titles for title in supporting):
            continue
        question = record["question"].strip()
        if not question.endswith("?"):
            question += "?"
        questions.append({
            "id": record["id"], "question": question, "outputs": record["golden_answers"],
            "context": [lookup[text] for text in documents(record)],
            "evidence_idx": [titles.index(title) for title in supporting],
            "level": metadata.get("level"), "type": metadata.get("type"),
        })
    return questions, corpus


def build_records(questions: list[dict[str, Any]], corpus: list[str], *, seed: int = 4,
                  sample_count: int = 128, num_docs: int = 50,
                  document_permutation_seed: int = 4) -> list[dict[str, Any]]:
    if sample_count < 1 or sample_count > len(questions):
        raise ValueError("sample_count must be positive and fit the eligible source")
    if not 1 <= num_docs <= len(corpus):
        raise ValueError("num_docs must fit the source corpus")
    rng = random.Random(seed)
    # The public script draws 5*n for distant-evidence filtering, then saves
    # the first n. Keep that exact draw (not a new sample(n) call).
    indices = rng.sample(range(len(questions)), min(5 * sample_count, len(questions)))
    result = []
    for index in indices[:sample_count]:
        question = questions[index]
        current = question["context"]
        if len(current) > num_docs:
            raise ValueError(f"question {question['id']} already exceeds num_docs")
        extra = question.get("more_context", [])
        missing = num_docs - len(current)
        if missing > len(extra):
            excluded = set(current + extra)
            candidates = [i for i in range(len(corpus)) if i not in excluded]
            docs = current + extra + rng.sample(candidates, missing - len(extra))
        else:
            docs = current + rng.sample(extra, missing)
        permutation = list(range(len(docs)))
        random.Random(document_permutation_seed).shuffle(permutation)
        doc_texts = [corpus[docs[i]] for i in permutation]
        evidence = [permutation.index(i) for i in question["evidence_idx"]]
        ordered_evidence = sorted(evidence)
        distances = [b - a for a, b in zip(ordered_evidence, ordered_evidence[1:])]
        result.append({
            "context": "\n\n".join(f"Document {i + 1}:\n{text}" for i, text in enumerate(doc_texts)),
            "input": question["question"], "answers": question["outputs"], "num_docs": num_docs,
            "index": index, "is_ordered": evidence == sorted(evidence),
            "is_reverse_ordered": evidence == sorted(evidence, reverse=True),
            "min_distance": min(distances) if distances else 0, "evidence_idx": evidence,
            "level": question["level"], "type": question["type"], "id": question["id"],
        })
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=4)
    parser.add_argument("--sample-count", type=int, default=128)
    parser.add_argument("--num-docs", type=int, default=50)
    args = parser.parse_args()
    source = load_source(args.source)
    questions, corpus = prepare_source(source)
    rows = build_records(questions, corpus, seed=args.seed, sample_count=args.sample_count, num_docs=args.num_docs)
    payload = (json.dumps(rows, ensure_ascii=False, indent=4) + "\n").encode()
    if args.output.exists() and args.output.read_bytes() != payload:
        raise ValueError("output contains a different frozen dataset; choose a new output path")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    provenance = {
        "source": str(args.source), "source_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
        "source_record_count": len(source), "eligible_question_count": len(questions), "corpus_documents": len(corpus),
        "selection_seed": args.seed, "distractor_seed": args.seed, "document_permutation_seed": 4,
        "question_draw_count": min(5 * args.sample_count, len(questions)),
        "rng_schedule": "one local Random(seed); question draw first, then distractors in selected question order",
        "output": str(args.output), "output_sha256": hashlib.sha256(payload).hexdigest(),
        "sample_count": len(rows), "num_docs": args.num_docs, "sample_ids": [row["id"] for row in rows],
        "generator": str(Path(__file__).relative_to(Path(__file__).resolve().parents[1])) if Path(__file__).is_absolute() else __file__,
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "paper_selection_seed": 4, "paper_exact_sample_match": False,
        "note": "Seeds match the paper settings. Author test IDs/context were not provided; exact paper sample identity remains unverified.",
    }
    manifest = args.output.with_suffix(".provenance.json")
    manifest.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"samples": len(rows), "documents": args.num_docs, "seed": args.seed,
                      "output_sha256": provenance["output_sha256"], "first_eight_ids": provenance["sample_ids"][:8]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
