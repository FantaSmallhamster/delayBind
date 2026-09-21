import hashlib

import pytest

from delaybind_core.archive import FutureSourceAccessError, SentenceArchive
from delaybind_core.cursor_v52 import SentenceReadCursor, TextReadCursor
from delaybind_core.data import build_manifest, canonicalize_record
from delaybind_core.sentence_index import index_manifest, sentence_spans, text_manifest
from delaybind_core.source_refs_v52 import SentenceRefResolver
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.token_budget import TokenCounter


class CharacterTokenizer:
    name = "test-unicode-characters"

    def encode(self, text, **kwargs):
        return list(text)

    def decode(self, ids, **kwargs):
        raise AssertionError("raw must never come from decode")


def test_fragment_roundtrip_immutable_anchors_and_restored_watermark():
    text = "Mary  was\tborn in Suzhou, not Shanghai. 下一句保持空格。\n"
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    counter = TokenCounter(CharacterTokenizer())
    cursor = TextReadCursor(text, archive, sample_id="s", counter=counter, window_mode="fragment")
    window = cursor.next_anchored_window(25)
    first = archive.fetch_sentence(window.source_refs[0])
    assert first.source_ref == "D0:S0:P1" and not first.complete
    with pytest.raises(FutureSourceAccessError):
        archive.fetch_sentence("D0:S0")
    saved = cursor.state()
    watermark = archive.watermark
    while not cursor.exhausted:
        cursor.next_anchored_window(25)
    assert archive.fetch_sentence(first.source_ref) == first
    complete = [archive.fetch_sentence(f"D0:S{i}") for i in range(len(sentence_spans(text)))]
    assert "".join(r.text for r in complete) == text
    assert all(r.text_sha256 == hashlib.sha256(r.text.encode()).hexdigest() for r in complete)
    restored = SentenceArchive(store, "r", watermark=watermark)
    with pytest.raises(FutureSourceAccessError):
        restored.fetch_sentence("D0:S0")
    resumed = TextReadCursor(text, restored, sample_id="s", counter=counter, window_mode="fragment", state=saved)
    while not resumed.exhausted:
        resumed.next_anchored_window(25)
    assert restored.fetch_sentence("D0:S0") == complete[0]


def test_title_fragment_and_complete_sentence_have_different_coordinate_spaces():
    sample = canonicalize_record({"id": "s", "question": "q", "context": [["A very long document title", ["Hi."]]]})
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    cursor = SentenceReadCursor(build_manifest(sample), archive, counter=TokenCounter(CharacterTokenizer()))
    while not cursor.exhausted:
        cursor.next_anchored_window(22)
    assert archive.anchor("D0:H0").coordinate_space == "document_title"
    assert archive.anchor("D0:S0").coordinate_space == "dataset_sentence"
    assert archive.fetch_sentence("D0:H0").text == sample.documents[0].title


@pytest.mark.parametrize("ref", ["D0", "Doc0", "F1", "D0:S9", "other:D0:S0", "D0@C0"])
def test_strict_visible_source_permissions(ref):
    with pytest.raises(ValueError):
        SentenceRefResolver(["D0:S0"]).resolve([ref])


def test_read_order_preserves_anchor_identity_and_dataset_mapping():
    sample = canonicalize_record({"id": "s", "question": "q", "supporting_facts": [["B", 0]],
                                  "context": [["A", ["a"]], ["B", ["b"]]]})
    a = index_manifest(build_manifest(sample, order="original"))
    b = index_manifest(build_manifest(sample, order="reverse"))
    assert {i.source_ref: i.entry.source_ref for i in a} == {i.source_ref: i.entry.source_ref for i in b}
    assert "supporting_facts" not in str([i.entry.model_dump() for i in a])


def test_raw_tampering_is_detected_and_neighbors_do_not_cross_document():
    sample = canonicalize_record({"id": "s", "question": "q", "context": [["A", ["a0", "a1"]], ["B", ["b0"]]]})
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    cursor = SentenceReadCursor(build_manifest(sample), archive, counter=TokenCounter(CharacterTokenizer()))
    cursor.next_anchored_window(1000)
    raw = archive.fetch_bounded_context("D0:S0", before=3, after=3)
    assert {r.source_ref for r in raw} == {"D0:H0", "D0:S0", "D0:S1"}
    block = archive.anchor("D0:S0").raw_block_refs[0]
    store.connection.execute("UPDATE raw_blocks SET text='tampered' WHERE block_id=?", (block,))
    store.connection.commit()
    with pytest.raises(ValueError, match="RAW_INTEGRITY_ERROR"):
        archive.fetch_sentence("D0:S0")
