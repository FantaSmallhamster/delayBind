"""Read-gated immutable raw archive."""

from __future__ import annotations

from typing import Iterable

from .manifest import ManifestEntry
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
