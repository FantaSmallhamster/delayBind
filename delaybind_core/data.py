"""Canonical loaders and manifest construction for 2Wiki/FlashRAG records."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any, Iterable, Iterator

from pydantic import BaseModel, ConfigDict, Field

from .manifest import Manifest, ManifestEntry


class CanonicalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    title: str
    sentences: list[str]


class CanonicalSample(BaseModel):
    model_config = ConfigDict(extra="allow")

    sample_id: str
    question: str
    answer: str | None = None
    answers: list[str] = Field(default_factory=list)
    question_type: str | None = None
    documents: list[CanonicalDocument]
    supporting_facts: list[tuple[str, int]] = Field(default_factory=list)
    evidences: list[list[Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _as_sentences(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(sentence) for sentence in value]
    return [str(value)]


def canonicalize_record(record: dict[str, Any], *, dataset_id: str = "2wiki") -> CanonicalSample:
    """Convert official 2Wiki or FlashRAG JSON into one internal shape."""
    metadata = record.get("metadata") or {}
    context = record.get("context") or metadata.get("context") or []
    documents: list[CanonicalDocument] = []
    if isinstance(context, dict):
        titles = context.get("title", [])
        contents = context.get("sentences", context.get("content", []))
        for index, (title, content) in enumerate(zip(titles, contents)):
            documents.append(
                CanonicalDocument(
                    document_id=f"d{index:05d}", title=str(title), sentences=_as_sentences(content)
                )
            )
    else:
        for index, item in enumerate(context):
            if isinstance(item, dict):
                title = item.get("title", f"d{index:05d}")
                content = item.get("sentences", item.get("content", []))
            else:
                title, content = item[0], item[1]
            documents.append(
                CanonicalDocument(
                    document_id=f"d{index:05d}", title=str(title), sentences=_as_sentences(content)
                )
            )

    sf = record.get("supporting_facts") or metadata.get("supporting_facts") or []
    supporting: list[tuple[str, int]] = []
    if isinstance(sf, dict):
        for title, sentence_id in zip(sf.get("title", []), sf.get("sent_id", sf.get("sentence_id", []))):
            supporting.append((str(title), int(sentence_id)))
    else:
        for item in sf:
            if len(item) >= 2:
                supporting.append((str(item[0]), int(item[1])))

    answers = record.get("golden_answers") or []
    answer = record.get("answer")
    if answer is not None and not answers:
        answers = [str(answer)]
    if not answer and answers:
        answer = str(answers[0])
    question_type = record.get("type") or metadata.get("type")
    return CanonicalSample(
        sample_id=str(record.get("_id") or record.get("id") or metadata.get("id") or "unknown"),
        question=str(record.get("question", "")).strip(),
        answer=str(answer) if answer is not None else None,
        answers=[str(item) for item in answers],
        question_type=str(question_type) if question_type is not None else None,
        documents=documents,
        supporting_facts=supporting,
        evidences=[list(item) for item in (record.get("evidences") or metadata.get("evidences") or [])],
        metadata={"dataset_id": dataset_id, **metadata},
    )


def load_records(path: str | Path, *, dataset_id: str = "2wiki") -> Iterator[CanonicalSample]:
    path = Path(path)
    if path.suffix == ".jsonl":
        records = (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    elif path.suffix == ".json":
        parsed = json.loads(path.read_text(encoding="utf-8"))
        records = iter(parsed if isinstance(parsed, list) else [parsed])
    else:
        raise ValueError(f"unsupported data format: {path}")
    for record in records:
        yield canonicalize_record(record, dataset_id=dataset_id)


def build_manifest(
    sample: CanonicalSample,
    *,
    dataset_id: str = "2wiki",
    seed: int = 4,
    order: str = "original",
    manifest_id: str | None = None,
) -> Manifest:
    """Build a deterministic sentence-level stream manifest.

    This function only changes presentation order.  It never changes the
    canonical sentence text or gold annotations.
    """
    documents = list(sample.documents)
    if order == "reverse":
        documents.reverse()
    elif order == "shuffle":
        random.Random(seed).shuffle(documents)
    elif order == "interleaved":
        return _build_interleaved_manifest(
            sample, documents=documents, dataset_id=dataset_id, seed=seed, manifest_id=manifest_id
        )
    elif order == "distant":
        documents = _distant_document_order(sample, documents)
    elif order != "original":
        raise ValueError(f"unsupported order: {order}")

    entries: list[ManifestEntry] = []
    stream_position = 0
    for document in documents:
        for sentence_id, text in enumerate(document.sentences):
            source_ref = f"{sample.sample_id}:{document.document_id}:s{sentence_id}"
            entries.append(
                ManifestEntry(
                    source_ref=source_ref,
                    dataset_id=dataset_id,
                    sample_id=sample.sample_id,
                    document_id=document.document_id,
                    title=document.title,
                    sentence_id=sentence_id,
                    text=text,
                    stream_position=stream_position,
                )
            )
            stream_position += 1

    if manifest_id is None:
        manifest_id = hashlib.sha256(
            json.dumps(
                {
                    "sample_id": sample.sample_id,
                    "seed": seed,
                    "order": order,
                    "source_refs": [entry.source_ref for entry in entries],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
    return Manifest.from_entries(
        manifest_id=manifest_id,
        dataset_id=dataset_id,
        sample_id=sample.sample_id,
        seed=seed,
        entries=entries,
        metadata={"order": order, "question_type": sample.question_type},
    )


def _distant_document_order(
    sample: CanonicalSample, documents: list[CanonicalDocument]
) -> list[CanonicalDocument]:
    """Place supporting documents at opposite ends around distractors.

    This creates a deterministic, document-level distant-evidence condition;
    it never changes sentence text or source IDs. With one supporting document,
    that document is placed first and distractors follow it.
    """
    support_titles = {title.casefold().strip() for title, _ in sample.supporting_facts}
    supporting = [doc for doc in documents if doc.title.casefold().strip() in support_titles]
    distractors = [doc for doc in documents if doc.title.casefold().strip() not in support_titles]
    if len(supporting) < 2:
        return supporting + distractors
    left = supporting[::2]
    right = supporting[1::2]
    return left + distractors + list(reversed(right))


def _build_interleaved_manifest(
    sample: CanonicalSample,
    *,
    documents: list[CanonicalDocument],
    dataset_id: str,
    seed: int,
    manifest_id: str | None,
) -> Manifest:
    """Round-robin document sentences to separate multi-hop evidence."""
    entries: list[ManifestEntry] = []
    stream_position = 0
    max_sentences = max((len(document.sentences) for document in documents), default=0)
    for sentence_id in range(max_sentences):
        for document in documents:
            if sentence_id >= len(document.sentences):
                continue
            entries.append(
                ManifestEntry(
                    source_ref=f"{sample.sample_id}:{document.document_id}:s{sentence_id}",
                    dataset_id=dataset_id,
                    sample_id=sample.sample_id,
                    document_id=document.document_id,
                    title=document.title,
                    sentence_id=sentence_id,
                    text=document.sentences[sentence_id],
                    stream_position=stream_position,
                )
            )
            stream_position += 1
    if manifest_id is None:
        manifest_id = hashlib.sha256(
            json.dumps(
                {
                    "sample_id": sample.sample_id,
                    "seed": seed,
                    "order": "interleaved",
                    "source_refs": [entry.source_ref for entry in entries],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]
    return Manifest.from_entries(
        manifest_id=manifest_id,
        dataset_id=dataset_id,
        sample_id=sample.sample_id,
        seed=seed,
        entries=entries,
        metadata={"order": "interleaved", "question_type": sample.question_type},
    )
