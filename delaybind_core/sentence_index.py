"""Deterministic sentence boundaries and order-independent short anchors."""

import re
from dataclasses import dataclass

from .manifest import Manifest, ManifestEntry

SEGMENTATION_VERSION = "syntok-1.4.4-analyze-lossless-v1"


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Use syntok's sentence offsets, partitioning every original character.

    Sentence decisions belong entirely to syntok.analyze, not our own rules.
    Keep leading whitespace in the first span, inter-sentence/paragraph gaps
    in the preceding span, and trailing whitespace in the last. Token offsets
    locate boundaries only; the caller always slices the untouched input.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    try:
        from syntok.segmenter import analyze
    except ImportError as exc:
        raise RuntimeError("Sentence splitting requires syntok==1.4.4; install delaybind-v5[v52] "
                           "or set sentence_splitting=false") from exc
    starts, previous_end = [], 0
    for paragraph in analyze(text):
        for tokens in paragraph:
            if not tokens:
                continue
            start = tokens[0].offset
            end = tokens[-1].offset + len(tokens[-1].value)
            if not previous_end <= start < end <= len(text):
                raise ValueError("INVALID_SYNTOK_OFFSETS")
            starts.append(start)
            previous_end = end
    if not text:
        return []
    # Even an all-whitespace document must remain reconstructible.
    boundaries = [0, *starts[1:], len(text)]
    return list(zip(boundaries, boundaries[1:]))


@dataclass(frozen=True)
class IndexedSentence:
    source_ref: str
    entry: ManifestEntry
    kind: str = "sentence"
    coordinate_space: str = "dataset_sentence"


def index_manifest(manifest: Manifest) -> list[IndexedSentence]:
    documents = sorted({e.document_id for e in manifest.entries})
    # Raw long-context inputs already carry stable document numbers. Preserve them.
    aliases = {doc: doc for doc in documents if re.fullmatch(r"D\d+", doc)}
    next_id = 0
    for doc in documents:
        if doc in aliases:
            continue
        while f"D{next_id}" in aliases.values():
            next_id += 1
        aliases[doc] = f"D{next_id}"
        next_id += 1
    explicit_headings = {e.document_id for e in manifest.entries if e.context_only}
    items, seen, titles = [], set(), {}
    for entry in manifest.entries:
        doc = aliases[entry.document_id]
        if entry.document_id in titles and titles[entry.document_id] != entry.title:
            raise ValueError("INCONSISTENT_DOCUMENT_TITLE")
        if entry.document_id not in titles:
            titles[entry.document_id] = entry.title
            if entry.title and entry.document_id not in explicit_headings:
                title = ManifestEntry(
                    **{**entry.model_dump(), "source_ref": f"{entry.source_ref}:heading",
                       "text": entry.title, "char_start": 0, "char_end": len(entry.title),
                       "text_sha256": None, "context_only": True}
                )
                items.append(IndexedSentence(f"{doc}:H0", title, "heading", "document_title"))
        kind = "heading" if entry.context_only else "sentence"
        ref = f"{doc}:{'H' if entry.context_only else 'S'}{entry.sentence_id}"
        if ref in seen:
            raise ValueError(f"DUPLICATE_SENTENCE_ID:{ref}")
        seen.add(ref)
        items.append(IndexedSentence(ref, entry, kind, entry.coordinate_space))
    return items


def text_manifest(text: str, *, sample_id: str, document_id: str = "D0") -> Manifest:
    headers = list(re.finditer(r"(?m)^Document[ \t]+(\d+):[ \t]*\r?\n", text))
    entries = []

    def append(doc, title, start, end, sid, *, heading=False, base=0):
        entries.append(ManifestEntry(
            source_ref=f"{sample_id}:{doc}:{'h' if heading else 's'}{sid}",
            dataset_id="text", sample_id=sample_id, document_id=doc, title=title,
            sentence_id=sid, text=text[start:end], stream_position=len(entries),
            char_start=start - base, char_end=end - base, coordinate_space="document",
            context_only=heading,
        ))

    def body(doc, title, start, end, base=0):
        for sid, (left, right) in enumerate(sentence_spans(text[start:end])):
            append(doc, title, start + left, start + right, sid, base=base)

    if not headers:
        body(document_id, "", 0, len(text))
    else:
        document_ids = [f"D{int(h.group(1))}" for h in headers]
        if len(document_ids) != len(set(document_ids)):
            raise ValueError("DUPLICATE_DOCUMENT_NUMBER")
        if headers[0].start():
            preamble_id = "D0"
            while preamble_id in document_ids:
                preamble_id = f"D{int(preamble_id[1:]) + 1}"
            body(preamble_id, "", 0, headers[0].start())
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            # The benchmark's first line after Document N is the title. Keep
            # the header and title as one exact original slice, never regenerate.
            newline = text.find("\n", header.end(), end)
            title_end = newline + 1 if newline >= 0 else end
            title = text[header.end():title_end].strip()
            doc, base = document_ids[index], header.start()
            append(doc, title, base, title_end, 0, heading=True, base=base)
            body(doc, title, title_end, end, base=base)
    return Manifest.from_entries(
        manifest_id=f"text-{sample_id}", dataset_id="text", sample_id=sample_id, seed=0,
        entries=entries, metadata={"segmentation_version": SEGMENTATION_VERSION},
    )
