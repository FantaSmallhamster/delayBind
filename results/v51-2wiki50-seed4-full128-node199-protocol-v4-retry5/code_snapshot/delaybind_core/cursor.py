"""A sentence-aware, forward-only read cursor."""

from __future__ import annotations

from dataclasses import dataclass
import re
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
        self._document_number: int | None = None
        self._seen_document_numbers: set[int] = set()
        self._header_carry = ""
        self._at_line_start = True
        self._source_position = 0
        self._decoded_position = 0

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
        entries = self._source_fragments(text)
        self.archive.append(entries)
        window = ReadWindow(f"c{self.window_index:05d}", tuple(entries), start, self.position - 1)
        self._decoded_position += len(text)
        self.window_index += 1
        return window

    def _source_fragments(self, text: str) -> list[ManifestEntry]:
        """Index only headers in the observed prefix, preserving token windows.

        Each document portion receives its own immutable source id. A header
        split by a token boundary is recognized only once its suffix is read.
        No unread body or future document title enters the archive.
        """
        prefix = self._header_carry or ("" if self._at_line_start else "\0")
        observed = prefix + text
        self._at_line_start = text.endswith("\n")
        headers = list(re.finditer(r"(?m)^Document[ \t]+([1-9][0-9]*)[ \t]*:[ \t]*\r?\n", observed))
        # Retain only an unfinished last-line header, never arbitrary old text.
        tail = observed.rsplit("\n", 1)[-1]
        self._header_carry = tail if (
            tail and len(tail) <= 80 and (
                "Document".startswith(tail) or re.fullmatch(r"Document[ \t]+[0-9]*[ \t]*:?[ \t]*\r?", tail)
            )
        ) else ""
        spans: list[tuple[int, int, int | None]] = []
        offset = 0
        number = self._document_number
        for header in headers:
            boundary = max(0, header.start() - len(prefix))
            if boundary > offset:
                spans.append((offset, boundary, number))
            number = int(header[1])
            if number in self._seen_document_numbers:
                raise ValueError(f"duplicate Document {number} header in raw context")
            self._seen_document_numbers.add(number)
            offset = boundary
        if offset < len(text):
            spans.append((offset, len(text), number))
        self._document_number = number
        entries = []
        for start, end, number in spans:
            suffix = f":D{number}" if number is not None else ""
            entries.append(ManifestEntry(
                source_ref=f"{self.sample_id}:c{self.window_index:05d}{suffix}",
                dataset_id=self.dataset_id, sample_id=self.sample_id,
                document_id=f"context:D{number}" if number is not None else "context",
                title=f"Document {number}" if number is not None else "",
                sentence_id=self.window_index, text=text[start:end],
                stream_position=self._source_position,
                char_start=self._decoded_position + start,
            ))
            self._source_position += 1
        return entries
