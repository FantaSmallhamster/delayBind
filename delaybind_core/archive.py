"""Read-gated immutable raw archive."""

from __future__ import annotations

from typing import Iterable
import hashlib

from .manifest import ManifestEntry, SentenceAnchor
from .schema_v52 import RawEvidence
from .storage import SQLiteEventStore


class FutureSourceAccessError(RuntimeError):
    """Raised when code asks for a span that has not entered the read prefix."""


class RawArchive:
    def __init__(self, store: SQLiteEventStore, run_id: str, *, session_prefix: bool = False):
        self.store = store
        self.run_id = run_id
        self._observed: set[str] | None = set() if session_prefix else None

    def append(self, entries: Iterable[ManifestEntry]) -> None:
        for entry in entries:
            existing = self.store.get_raw_span(self.run_id, entry.source_ref)
            if existing is not None and existing != entry:
                raise ValueError(f"source content changed for {entry.source_ref}; use a new run_id")
            self.store.append_raw_span(self.run_id, entry)
            if self._observed is not None:
                self._observed.add(entry.source_ref)

    def contains(self, source_ref: str) -> bool:
        return (self._observed is None or source_ref in self._observed) and self.store.raw_span_exists(self.run_id, source_ref)

    def entry(self, source_ref: str) -> ManifestEntry:
        entry = self.store.get_raw_span(self.run_id, source_ref)
        if entry is None or self._observed is not None and source_ref not in self._observed:
            raise FutureSourceAccessError(
                f"source_ref {source_ref!r} is not in the read prefix for run {self.run_id}"
            )
        return entry

    def fetch(self, source_ref: str, *, neighborhood: int = 0) -> list[ManifestEntry]:
        self.entry(source_ref)
        return [entry for entry in self.store.fetch_raw_neighborhood(self.run_id, source_ref, neighborhood)
                if self._observed is None or entry.source_ref in self._observed]

    def expanded_context(
        self,
        source_ref: str,
        *,
        mentions: list[str],
        limit: int = 32,
    ) -> list[ManifestEntry]:
        """Return same-document context plus read-prefix entity mentions."""
        self.entry(source_ref)
        entries = [
            *self.store.fetch_raw_document(self.run_id, source_ref),
            *self.store.search_raw_mentions(self.run_id, mentions, limit=limit),
        ]
        by_ref = {entry.source_ref: entry for entry in entries
                  if self._observed is None or entry.source_ref in self._observed}
        return sorted(by_ref.values(), key=lambda entry: entry.stream_position)[:limit]

    def search_mentions(self, mentions: list[str], *, limit: int = 32) -> list[ManifestEntry]:
        """Search only the current read prefix by sentence text or document title."""
        return [entry for entry in self.store.search_raw_mentions(self.run_id, mentions, limit=limit)
                if self._observed is None or entry.source_ref in self._observed]

class SentenceArchive:
    """V5.2 archive capability. No keyword search; explicit restored watermark."""

    def __init__(self, store: SQLiteEventStore, run_id: str, *, watermark: int = -1):
        self.store, self.run_id, self.watermark = store, run_id, watermark

    def append_observed(self, blocks, anchors, *, window_index: int) -> None:
        if window_index != self.watermark + 1:
            raise ValueError("NONCONTIGUOUS_READ_WATERMARK")
        self.store.append_observed_raw(self.run_id, blocks, anchors)
        self.watermark = window_index

    def anchor(self, ref: str) -> SentenceAnchor:
        row = self.store.connection.execute(
            "SELECT payload_json FROM raw_sentences WHERE run_id=? AND source_ref=? AND observed_window<=?",
            (self.run_id, ref, self.watermark)).fetchone()
        if row is None:
            raise FutureSourceAccessError(f"SOURCE_NOT_READ:{ref}")
        return SentenceAnchor.model_validate_json(row[0])

    def fetch_sentence(self, ref: str) -> RawEvidence:
        a = self.anchor(ref)
        parts = []
        for block_id in a.raw_block_refs:
            row = self.store.connection.execute(
                "SELECT text,text_sha256 FROM raw_blocks WHERE run_id=? AND block_id=?",
                (self.run_id, block_id)).fetchone()
            if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
                raise ValueError(f"RAW_INTEGRITY_ERROR:{ref}")
            parts.append(row[0])
        text = "".join(parts)
        if hashlib.sha256(text.encode()).hexdigest() != a.text_sha256:
            raise ValueError(f"RAW_INTEGRITY_ERROR:{ref}")
        if len(text) != a.original_char_end - a.original_char_start:
            raise ValueError(f"RAW_COORDINATE_ERROR:{ref}")
        return RawEvidence(source_ref=ref, text=text, kind=a.kind, complete=a.complete,
                           text_sha256=a.text_sha256)

    def fetch_sentences(self, refs) -> list[RawEvidence]:
        return [self.fetch_sentence(ref) for ref in sorted(set(refs))]

    def fetch_bounded_context(self, ref: str, *, before: int, after: int) -> list[RawEvidence]:
        source = self.anchor(ref)
        rows = self.store.connection.execute(
            "SELECT payload_json FROM raw_sentences WHERE run_id=? AND observed_window<=?",
            (self.run_id, self.watermark))
        refs = [ref]
        for row in rows:
            a = SentenceAnchor.model_validate_json(row[0])
            if (a.document_id == source.document_id and a.complete and
                (a.kind == "heading" or source.sentence_id - before <= a.sentence_id <= source.sentence_id + after)):
                refs.append(a.source_ref)
        return self.fetch_sentences(refs)
