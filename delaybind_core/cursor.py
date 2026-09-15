"""A sentence-aware, forward-only read cursor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

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


class TextTokenizer(Protocol):
    def encode(self, text: str, **kwargs: Any) -> list[int]: ...
    def decode(self, tokens: list[int], **kwargs: Any) -> str: ...


class TextReadCursor:
    """ReMemR1-style question/context input, with source ids assigned on read.

    With a tokenizer, chunk_size measures that tokenizer's tokens. Without
    one, chunks use Unicode characters and report that unit explicitly.
    """

    def __init__(self, context: str, archive: RawArchive, *, sample_id: str,
                 dataset_id: str = "text", tokenizer: TextTokenizer | None = None):
        self.archive = archive
        self.sample_id = sample_id
        self.dataset_id = dataset_id
        self.tokenizer = tokenizer
        self.units = tokenizer.encode(context) if tokenizer else context
        self.budget_unit = "tokens" if tokenizer else "characters"
        self.position = 0
        self.window_index = 0

    @property
    def exhausted(self) -> bool:
        return self.position >= len(self.units)

    def next_window(self, token_budget: int = 5000) -> ReadWindow | None:
        if token_budget <= 0:
            raise ValueError("chunk_size must be positive")
        if self.exhausted:
            return None
        start = self.position
        self.position = min(len(self.units), start + token_budget)
        chunk = self.units[start:self.position]
        text = self.tokenizer.decode(chunk) if self.tokenizer else chunk
        entry = ManifestEntry(
            source_ref=f"{self.sample_id}:c{self.window_index:05d}",
            dataset_id=self.dataset_id, sample_id=self.sample_id,
            document_id="context", title="", sentence_id=self.window_index,
            text=text, stream_position=self.window_index,
        )
        self.archive.append([entry])
        window = ReadWindow(f"c{self.window_index:05d}", (entry,), start, self.position - 1)
        self.window_index += 1
        return window
