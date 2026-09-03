"""A sentence-aware, forward-only read cursor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .archive import RawArchive
from .manifest import Manifest, ManifestEntry


@dataclass(frozen=True)
class ReadWindow:
    window_id: str
    entries: tuple[ManifestEntry, ...]
    start_position: int
    end_position: int


class ReadCursor:
    def __init__(self, manifest: Manifest, archive: RawArchive):
        self.manifest = manifest
        self.archive = archive
        self.position = 0
        self.window_index = 0

    @property
    def exhausted(self) -> bool:
        return self.position >= len(self.manifest.entries)

    def next_window(self, token_budget: int = 5000) -> ReadWindow | None:
        if self.exhausted:
            return None
        start = self.position
        used = 0
        selected: list[ManifestEntry] = []
        while self.position < len(self.manifest.entries):
            entry = self.manifest.entries[self.position]
            estimate = max(1, len(entry.text.split()))
            if selected and used + estimate > token_budget:
                break
            selected.append(entry)
            used += estimate
            self.position += 1
        self.archive.append(selected)
        window = ReadWindow(
            window_id=f"w{self.window_index:05d}",
            entries=tuple(selected),
            start_position=start,
            end_position=self.position - 1,
        )
        self.window_index += 1
        return window
