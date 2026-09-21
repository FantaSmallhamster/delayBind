"""V5.1 windowing adapted to the V5.2 read-gated, immutable archive.

No punctuation segmentation is performed. Raw text uses the actual V5.1
TextReadCursor; explicit manifests use its ReadCursor and existing entry units.
"""

import re

from .cursor import ReadCursor, TextReadCursor
from .cursor_v52 import AnchoredWindow
from .manifest import SentenceAnchor
from .schema_v52 import digest
from .sentence_index import index_manifest


LEGACY_SEGMENTATION_VERSION = "none-v51-windows-v1"


class _EntrySink:
    def append(self, entries):
        # The legacy cursor returns these same entries in ReadWindow. They are
        # persisted only after integrity checks in next_anchored_window below.
        pass


class LegacyReadCursor:
    def __init__(self, archive, *, text=None, sample_id=None, manifest=None, tokenizer=None, state=None):
        self.archive, self.text = archive, text
        self.is_text = manifest is None
        self.tokenizer_identity = (getattr(tokenizer, "name_or_path", getattr(tokenizer, "name", type(tokenizer).__name__))
                                   if tokenizer is not None else "unicode-characters")
        if self.is_text:
            if text is None:
                raise ValueError("LEGACY_CURSOR_REQUIRES_TEXT_OR_MANIFEST")
            self.cursor = TextReadCursor(text, _EntrySink(), sample_id=sample_id, tokenizer=tokenizer)
            self.source_version = digest([LEGACY_SEGMENTATION_VERSION, sample_id, text])
        else:
            self.cursor = ReadCursor(manifest, _EntrySink())
            self.source_version = digest([LEGACY_SEGMENTATION_VERSION, manifest.model_dump(mode="json")])
            original_refs = {e.source_ref for e in manifest.entries}
            self.index = {i.entry.source_ref: i for i in index_manifest(manifest)
                          if i.entry.source_ref in original_refs}
        if state:
            if (state.get("source_version") != self.source_version
                    or state.get("segmentation_version") != LEGACY_SEGMENTATION_VERSION
                    or state.get("tokenizer") != self.tokenizer_identity
                    or state.get("input_kind") != ("text" if self.is_text else "manifest")):
                raise ValueError("CURSOR_SOURCE_OR_MODE_CHANGED")
            for name, value in state["legacy_state"].items():
                setattr(self.cursor, name, set(value) if name == "_seen_document_numbers" else value)
            if archive.watermark != self.window_index - 1:
                raise ValueError("CURSOR_WATERMARK_MISMATCH")

    @property
    def exhausted(self):
        return self.cursor.exhausted

    @property
    def window_index(self):
        return self.cursor.window_index

    def state(self):
        names = ["position", "window_index"]
        if self.is_text:
            names += ["_document_number", "_seen_document_numbers", "_header_carry", "_at_line_start",
                      "_source_position", "_decoded_position"]
        legacy_state = {n: sorted(getattr(self.cursor, n)) if isinstance(getattr(self.cursor, n), set)
                        else getattr(self.cursor, n) for n in names}
        return dict(source_version=self.source_version, window_index=self.window_index,
                    window_mode="legacy", segmentation_version=LEGACY_SEGMENTATION_VERSION,
                    tokenizer=self.tokenizer_identity, input_kind="text" if self.is_text else "manifest",
                    budget_unit=(self.cursor.budget_unit if self.is_text else "words"), legacy_state=legacy_state)

    def next_anchored_window(self, token_budget):
        if token_budget <= 0:
            raise ValueError("chunk_size must be positive")
        index = self.window_index
        window = self.cursor.next_window(token_budget)
        if window is None:
            return None
        blocks, anchors, refs = [], [], []
        for entry in window.entries:
            if self.is_text:
                # Legacy token decode may normalize text or split a Unicode
                # character. Fail explicitly instead of certifying changed text
                # as verbatim evidence. Normal lossless windows are identical.
                if self.text[entry.char_start:entry.char_end] != entry.text:
                    raise ValueError("LEGACY_CHUNK_NOT_VERBATIM: tokenizer decode changed an original slice; "
                                     "enable sentence_splitting or use a lossless chunk boundary")
                match = re.fullmatch(r"context:D([0-9]+)", entry.document_id)
                doc = f"D{match[1]}" if match else "D0"
                ref, kind, coordinate = f"{doc}@C{index}", "chunk", "context"
            else:
                item = self.index[entry.source_ref]
                ref, kind, coordinate = item.source_ref, item.kind, item.coordinate_space
                doc = entry.document_id
            block_id = f"{self.source_version}:{ref}"
            blocks.append((block_id, entry.text, entry.text_sha256))
            anchors.append(SentenceAnchor(
                source_ref=ref, source_version=self.source_version, document_id=doc,
                sentence_id=entry.sentence_id, kind=kind, coordinate_space=coordinate,
                original_char_start=entry.char_start, original_char_end=entry.char_end,
                raw_block_refs=(block_id,), text_sha256=entry.text_sha256,
                first_seen_window=index, segmentation_version=LEGACY_SEGMENTATION_VERSION,
                complete=True, original_source_ref=entry.source_ref,
            ))
            refs.append(ref)
        self.archive.append_observed(blocks, anchors, window_index=index)
        return AnchoredWindow(index, tuple(refs), ())
