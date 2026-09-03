"""Read-gated immutable raw archive."""

from __future__ import annotations

from typing import Iterable

from .manifest import ManifestEntry
from .storage import SQLiteEventStore


class FutureSourceAccessError(RuntimeError):
    """Raised when code asks for a span that has not entered the read prefix."""


class RawArchive:
    def __init__(self, store: SQLiteEventStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def append(self, entries: Iterable[ManifestEntry]) -> None:
        for entry in entries:
            self.store.append_raw_span(self.run_id, entry)

    def contains(self, source_ref: str) -> bool:
        return self.store.raw_span_exists(self.run_id, source_ref)

    def entry(self, source_ref: str) -> ManifestEntry:
        entry = self.store.get_raw_span(self.run_id, source_ref)
        if entry is None:
            raise FutureSourceAccessError(
                f"source_ref {source_ref!r} is not in the read prefix for run {self.run_id}"
            )
        return entry

    def fetch(self, source_ref: str, *, neighborhood: int = 0) -> list[ManifestEntry]:
        self.entry(source_ref)
        return self.store.fetch_raw_neighborhood(self.run_id, source_ref, neighborhood)

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
        by_ref = {entry.source_ref: entry for entry in entries}
        return sorted(by_ref.values(), key=lambda entry: entry.stream_position)[:limit]
