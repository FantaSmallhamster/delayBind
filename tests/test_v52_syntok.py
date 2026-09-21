import builtins

import pytest
import syntok.segmenter as segmenter

from delaybind_core.archive import SentenceArchive
from delaybind_core.cursor_v52 import TextReadCursor
from delaybind_core.sentence_index import SEGMENTATION_VERSION, sentence_spans, text_manifest
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.token_budget import TokenCounter
from test_v52_sources import CharacterTokenizer


def test_no_space_sentence_boundaries_and_person_initials():
    text = ("She was a writer.Born in London, she moved abroad."
            "She worked with Joseph N. Ermolieff.")
    assert [text[a:b] for a, b in sentence_spans(text)] == [
        "She was a writer.", "Born in London, she moved abroad.",
        "She worked with Joseph N. Ermolieff.",
    ]


@pytest.mark.parametrize("text", [
    "", " \t\r\n  ", "  First sentence.\t Second sentence.\r\n\r\nThird paragraph.  \n",
    "Don't join hyphen-\nated words. Next sentence.",
    "Joseph N. Ermolieff was a producer.He lived in Paris.",
    'He said, "Leave now!" She left.\n\nA new paragraph.',
    "Mary  was\tborn in Suzhou, not Shanghai. 下一句保持空格。\n",
    "An unsplit long sentence " * 100,
])
def test_offsets_match_library_and_preserve_all_original_characters(text):
    expected_starts = [tokens[0].offset for paragraph in segmenter.analyze(text) for tokens in paragraph if tokens]
    spans = sentence_spans(text)
    assert "".join(text[a:b] for a, b in spans) == text
    assert [a for a, _ in spans[1:]] == expected_starts[1:]
    assert all(a < b for a, b in spans)
    assert all(left[1] == right[0] for left, right in zip(spans, spans[1:]))
    manifest = text_manifest(text, sample_id="s")
    assert "".join(e.text for e in manifest.entries) == text
    assert manifest.metadata["segmentation_version"] == SEGMENTATION_VERSION


@pytest.mark.parametrize("value", [None, 123, b"bytes"])
def test_non_string_input_is_rejected(value):
    with pytest.raises(TypeError, match="text must be a string"):
        sentence_spans(value)


def test_wrapper_uses_analyze_not_process_and_keeps_whitespace(monkeypatch):
    actual_analyze = segmenter.analyze
    calls = []

    def analyze(text):
        calls.append(text)
        return actual_analyze(text)

    def forbidden(*args, **kwargs):
        raise AssertionError("process must not normalize source text")

    monkeypatch.setattr(segmenter, "analyze", analyze)
    monkeypatch.setattr(segmenter, "process", forbidden)
    text = "  Don't change hyphen-\nated text.\n\nNext sentence.  "
    assert "".join(text[a:b] for a, b in sentence_spans(text)) == text
    assert calls == [text]


def test_old_segmentation_cannot_resume_even_with_same_source_hash():
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    counter = TokenCounter(CharacterTokenizer())
    cursor = TextReadCursor("One complete sentence. Another complete sentence.", archive, sample_id="s", counter=counter)
    cursor.next_anchored_window(40)
    state = cursor.state()
    state["segmentation_version"] = "unicode-punctuation-v2-numbered-documents"
    with pytest.raises(ValueError, match="CURSOR_SOURCE_OR_MODE_CHANGED"):
        TextReadCursor("One complete sentence. Another complete sentence.", archive,
                       sample_id="s", counter=counter, state=state)


def test_missing_library_gives_install_hint_instead_of_regex_fallback(monkeypatch):
    original_import = builtins.__import__

    def importing(name, *args, **kwargs):
        if name.startswith("syntok"):
            raise ImportError("intentionally unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(RuntimeError, match="syntok==1.4.4"):
        sentence_spans("First sentence.Second sentence.")
