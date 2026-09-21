"""Forward-only original-character cursor, with persistent sentence fragments."""

import hashlib
from dataclasses import dataclass

from .archive import SentenceArchive
from .manifest import Manifest, SentenceAnchor
from .schema_v52 import digest
from .sentence_index import SEGMENTATION_VERSION, index_manifest, text_manifest
from .token_budget import TokenCounter


@dataclass(frozen=True)
class AnchoredWindow:
    window_index: int
    source_refs: tuple[str, ...]
    completed_refs: tuple[str, ...]


class SentenceReadCursor:
    def __init__(self, manifest: Manifest, archive: SentenceArchive, *, counter: TokenCounter,
                 window_mode: str = "sentence", state: dict | None = None):
        if window_mode not in {"sentence", "fragment"}:
            raise ValueError("INVALID_WINDOW_MODE")
        self.items = index_manifest(manifest)
        self.source_version = digest(manifest.model_dump(mode="json"))
        self.archive, self.counter, self.window_mode = archive, counter, window_mode
        self.position = self.offset = self.window_index = 0
        self.parts = []
        if state:
            if (state["source_version"] != self.source_version or state["window_mode"] != window_mode
                    or state.get("segmentation_version") != SEGMENTATION_VERSION
                    or state["tokenizer"] != counter.identity):
                raise ValueError("CURSOR_SOURCE_OR_MODE_CHANGED")
            self.position, self.offset = state["position"], state["offset"]
            self.window_index, self.parts = state["window_index"], list(state["parts"])
            if archive.watermark != self.window_index - 1:
                raise ValueError("CURSOR_WATERMARK_MISMATCH")

    @property
    def exhausted(self):
        return self.position >= len(self.items)

    def state(self):
        return dict(source_version=self.source_version, position=self.position, offset=self.offset,
                    window_index=self.window_index, parts=list(self.parts), window_mode=self.window_mode,
                    segmentation_version=SEGMENTATION_VERSION, tokenizer=self.counter.identity)

    def next_anchored_window(self, token_budget: int) -> AnchoredWindow | None:
        if self.exhausted:
            return None
        blocks, anchors, visible, completed = [], [], [], []
        rendered = ""
        while not self.exhausted:
            item = self.items[self.position]
            text, start = item.entry.text, self.offset
            whole_label = item.source_ref
            remainder = text[start:]
            next_label = f"{whole_label}:P{len(self.parts) + 1}" if self.parts else whole_label
            candidate = rendered + f"[{next_label}] {remainder}\n"
            if self.counter.count(candidate) <= token_budget:
                end = len(text)
            elif rendered and self.window_mode == "sentence" and not self.parts:
                break
            else:
                label = f"{whole_label}:P{len(self.parts) + 1}"
                lo, hi = start, len(text)
                # All budget decisions are validated by encode of original slices.
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if self.counter.count(rendered + f"[{label}] {text[start:mid]}\n") <= token_budget:
                        lo = mid
                    else:
                        hi = mid - 1
                end = lo
                if end == start:
                    if rendered:
                        break
                    raise ValueError("WINDOW_ANCHOR_BUDGET")
            fragmented = bool(self.parts) or end < len(text)
            ref = f"{whole_label}:P{len(self.parts) + 1}" if fragmented else whole_label
            raw = text[start:end]
            block_id = f"{self.source_version}:{ref}"
            sha = hashlib.sha256(raw.encode()).hexdigest()
            blocks.append((block_id, raw, sha))
            anchors.append(SentenceAnchor(
                source_ref=ref, source_version=self.source_version, document_id=item.entry.document_id,
                sentence_id=item.entry.sentence_id, kind="fragment" if fragmented else item.kind,
                coordinate_space=item.coordinate_space,
                original_char_start=item.entry.char_start + start,
                original_char_end=item.entry.char_start + end, raw_block_refs=(block_id,),
                text_sha256=sha, first_seen_window=self.window_index,
                segmentation_version=SEGMENTATION_VERSION, complete=not fragmented,
                original_source_ref=item.entry.source_ref,
            ))
            self.parts.append(block_id)
            visible.append(ref)
            rendered += f"[{ref}] {raw}\n"
            if end == len(text):
                if fragmented:
                    anchors.append(SentenceAnchor(
                        source_ref=whole_label, source_version=self.source_version,
                        document_id=item.entry.document_id, sentence_id=item.entry.sentence_id,
                        kind=item.kind, coordinate_space=item.coordinate_space,
                        original_char_start=item.entry.char_start, original_char_end=item.entry.char_end,
                        raw_block_refs=tuple(self.parts), text_sha256=item.entry.text_sha256,
                        first_seen_window=self.window_index, segmentation_version=SEGMENTATION_VERSION,
                        original_source_ref=item.entry.source_ref,
                    ))
                    completed.append(whole_label)
                self.position += 1
                self.offset, self.parts = 0, []
            else:
                self.offset = end
                break
        self.archive.append_observed(blocks, anchors, window_index=self.window_index)
        window = AnchoredWindow(self.window_index, tuple(visible), tuple(completed))
        self.window_index += 1
        return window


class TextReadCursor(SentenceReadCursor):
    def __init__(self, text: str, archive: SentenceArchive, *, sample_id: str, **kwargs):
        super().__init__(text_manifest(text, sample_id=sample_id), archive, **kwargs)
