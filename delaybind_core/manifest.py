"""Immutable sentence-level experiment manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ref: str
    dataset_id: str
    sample_id: str
    document_id: str
    title: str
    sentence_id: int
    text: str
    stream_position: int
    char_start: int = 0
    char_end: int | None = None
    text_sha256: str | None = None
    context_only: bool = False

    @model_validator(mode="after")
    def ensure_hash(self) -> "ManifestEntry":
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.text_sha256 is None:
            self.text_sha256 = digest
        elif self.text_sha256 != digest:
            raise ValueError(f"text_sha256 mismatch for {self.source_ref}")
        if self.char_end is None:
            self.char_end = self.char_start + len(self.text)
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    dataset_id: str
    sample_id: str
    seed: int
    entries: list[ManifestEntry] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_order(self) -> "Manifest":
        positions = [entry.stream_position for entry in self.entries]
        if positions != sorted(positions) or len(positions) != len(set(positions)):
            raise ValueError("manifest entries must have unique ascending stream_position")
        for entry in self.entries:
            if entry.dataset_id != self.dataset_id or entry.sample_id != self.sample_id:
                raise ValueError("entry dataset/sample does not match manifest")
        return self

    @classmethod
    def from_json(cls, path: str | Path) -> "Manifest":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def to_json(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2), encoding="utf-8")

    def by_ref(self, source_ref: str) -> ManifestEntry:
        for entry in self.entries:
            if entry.source_ref == source_ref:
                return entry
        raise KeyError(source_ref)

    @classmethod
    def from_entries(
        cls,
        *,
        manifest_id: str,
        dataset_id: str,
        sample_id: str,
        seed: int,
        entries: Iterable[ManifestEntry],
        metadata: dict[str, object] | None = None,
    ) -> "Manifest":
        return cls(
            manifest_id=manifest_id,
            dataset_id=dataset_id,
            sample_id=sample_id,
            seed=seed,
            entries=list(entries),
            metadata=metadata or {},
        )
