import asyncio
import json
import re

import pytest

from delaybind_core.archive import FutureSourceAccessError, RawArchive
from delaybind_core.cursor import TextReadCursor
from delaybind_core.fact_protocol import ProtocolError, parse_update
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema import QueryPlan
from delaybind_core.source_refs import VisibleSources, source_label
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.replay import replay_events


def make_cursor(text):
    store = SQLiteEventStore()
    archive = RawArchive(store, "source-test", session_prefix=True)
    return TextReadCursor(text, archive, sample_id="sample"), archive


@pytest.mark.parametrize("label", ["Doc44", "Document 44", "doc_44", "D44", "44", "[D44]", "D44@C0"])
def test_observed_document_labels_resolve_to_exact_source_fragment(label):
    text = "Document 44:\nFilm\nThe film was released in 2007.\n\nDocument 45:\nOther\nUnrelated fact."
    cursor, archive = make_cursor(text)
    window = cursor.next_window(1000)
    assert "".join(entry.text for entry in window.entries) == text
    sources = VisibleSources(entry.model_dump() for entry in window.entries)
    update, mappings = sources.normalize_update(parse_update(f"Q1 | {label} | The film was released in 2007. | ACTIVE"))
    assert update.facts[0].source_refs == ["sample:c00000:D44"]
    fetched = archive.fetch(update.facts[0].source_refs[0])
    assert len(fetched) == 1
    assert "released in 2007" in fetched[0].text
    assert "Unrelated fact" not in fetched[0].text
    assert list(mappings.values()) == [["sample:c00000:D44"]]


def test_cross_window_fragments_do_not_reveal_future_or_unshown_history():
    first_text = "Document 44:\nPerson\nAlice's mother is Mary.\n"
    rest = "Mary was born in Suzhou.\n\nDocument 45:\nCindy\nCindy's teacher is Alice."
    cursor, archive = make_cursor(first_text + rest)
    first = cursor.next_window(len(first_text))
    sources = VisibleSources(entry.model_dump() for entry in first.entries)
    for ref in ["D45", "D44@C1", "sample:c00001:D44", "NONE"]:
        with pytest.raises(ProtocolError):
            sources.resolve(ref)
    with pytest.raises(FutureSourceAccessError):
        archive.fetch("sample:c00001:D44")
    assert archive.fetch("sample:c00000:D44")[0].text == first_text

    second = cursor.next_window(1000)
    assert [source_label(entry.model_dump()) for entry in second.entries] == ["D44@C1", "D45@C1"]
    current = VisibleSources(entry.model_dump() for entry in second.entries)
    assert current.resolve("Doc44") == ["sample:c00001:D44"]
    # The first fragment is in the archive, but was not passed to this view.
    with pytest.raises(ProtocolError):
        current.resolve("D44@C0")
    both = VisibleSources(entry.model_dump() for entry in (*first.entries, *second.entries))
    assert both.resolve("Doc44") == ["sample:c00000:D44", "sample:c00001:D44"]
    assert "Mary was born in Suzhou" not in archive.fetch("sample:c00000:D44")[0].text


@pytest.mark.parametrize("split", list(range(1, len("Document 44:\n"))))
def test_document_header_split_at_any_character_is_recognized_after_read(split):
    text = "Document 44:\nAlice\nA fact."
    cursor, archive = make_cursor(text)
    first = cursor.next_window(split)
    assert not VisibleSources(entry.model_dump() for entry in first.entries).by_document
    second = cursor.next_window(1000)
    sources = VisibleSources(entry.model_dump() for entry in second.entries)
    assert sources.resolve("44") == ["sample:c00001:D44"]
    assert "".join(entry.text for entry in (*first.entries, *second.entries)) == text


def test_token_window_boundaries_and_raw_char_offsets_are_preserved():
    class Tokenizer:
        def encode(self, text):
            return [ord(char) for char in text]

        def decode(self, tokens):
            return "".join(chr(token) for token in tokens)

    text = "Document 1:\n标题\n第一句。\n\nDocument 2:\n另一标题\n第二句。"
    archive = RawArchive(SQLiteEventStore(), "offsets")
    cursor = TextReadCursor(text, archive, sample_id="s", tokenizer=Tokenizer())
    windows = []
    entries = []
    while not cursor.exhausted:
        window = cursor.next_window(7)
        windows.append("".join(entry.text for entry in window.entries))
        entries.extend(window.entries)
    assert windows == [text[i:i + 7] for i in range(0, len(text), 7)]
    assert len({entry.source_ref for entry in entries}) == len(entries)
    assert [entry.stream_position for entry in entries] == list(range(len(entries)))
    for entry in entries:
        assert text[entry.char_start:entry.char_end] == entry.text


def test_mid_sentence_window_boundary_does_not_invent_document_header():
    prefix = "The words are "
    cursor, _ = make_cursor(prefix + "Document 44:\nThis is ordinary prose.")
    cursor.next_window(len(prefix))
    second = cursor.next_window(1000)
    assert not VisibleSources(entry.model_dump() for entry in second.entries).by_document


def test_duplicate_document_numbers_are_rejected_instead_of_merging_sources():
    cursor, _ = make_cursor("Document 1:\nA\nfact\n\nDocument 1:\nB\ndifferent fact")
    with pytest.raises(ValueError, match="duplicate Document 1"):
        cursor.next_window(1000)


def test_aliases_do_not_match_unknown_documents_or_arbitrary_chunk_numbers():
    cursor, _ = make_cursor("Document 44:\nFilm\nA fact.")
    window = cursor.next_window(1000)
    sources = VisibleSources(entry.model_dump() for entry in window.entries)
    for ref in ["Doc45", "doc_0", "4", "NONE", "c00000", "Doc44 and Doc45"]:
        with pytest.raises(ProtocolError):
            sources.resolve(ref)
    cursor, _ = make_cursor("Unnumbered text containing the number 44.")
    window = cursor.next_window(1000)
    with pytest.raises(ProtocolError):
        VisibleSources(entry.model_dump() for entry in window.entries).resolve("44")


def test_ambiguous_document_number_from_different_samples_is_not_merged():
    cursor, _ = make_cursor("Document 44:\nA\nfact")
    entry = cursor.next_window(1000).entries[0].model_dump()
    other = {**entry, "source_ref": "other:c00000:D44", "sample_id": "other"}
    sources = VisibleSources([entry, other])
    for ref in ["Doc44", "D44@C0"]:
        with pytest.raises(ProtocolError):
            sources.resolve(ref)
    assert sources.resolve(entry["source_ref"]) == [entry["source_ref"]]


def test_multiple_sources_plan_hints_and_alias_deduplication():
    cursor, _ = make_cursor("Document 44:\nA\nfact\n\nDocument 45:\nB\nother fact")
    window = cursor.next_window(1000)
    sources = VisibleSources(entry.model_dump() for entry in window.entries)
    update, _ = sources.normalize_update(parse_update(
        "Q1 | Doc44,44,Document 45 | Complete fact. | ACTIVE\n"
        "PLAN_HINT | doc_45 | A related fact outside the plan."
    ))
    assert update.facts[0].source_refs == ["sample:c00000:D44", "sample:c00000:D45"]
    assert update.hints[0].source_refs == ["sample:c00000:D45"]


def test_copied_header_and_explicit_no_evidence_rows_do_not_destroy_valid_facts():
    update = parse_update(
        "query_id | source_ref1,source_ref2 | complete natural-language fact | ACTIVE or DORMANT\n"
        "Q1 | doc_41 | A factual statement. | ACTIVE\n"
        "NONE | | No source states an answer. | DORMANT\n"
        "NONE"
    )
    assert len(update.facts) == 1 and update.facts[0].source_refs == ["doc_41"]
    assert len(update.ignored_lines) == 3
    # The combined action is never accepted on an actual fact line.
    with pytest.raises(ProtocolError):
        parse_update("Q1 | doc_41 | A fact. | ACTIVE or DORMANT")


def test_document_aliases_reach_memory_binding_defer_verify_and_answer():
    plan = QueryPlan(plan_id="chain", queries=[
        {"id": "Q1", "template": "Who is Cindy's teacher?", "output": "?teacher"},
        {"id": "Q2", "template": "Who is ?teacher's mother?", "output": "?mother", "depends_on": ["Q1"]},
    ])
    first = "Document 44:\nAlice\nAlice's mother is Mary.\n\n"
    second = "Document 45:\nCindy\nCindy's teacher is Alice."

    class Client:
        def __init__(self):
            self.calls = []

        async def complete(self, *, interface, messages, **kwargs):
            self.calls.append(interface)
            prompt = messages[0]["content"]
            if interface == "UPDATE":
                if "[D44@C0]" in prompt:
                    return "Q2 | doc_44 | Alice's mother is Mary. | DORMANT"
                assert "[D45@C1]" in prompt
                return "Q1 | Document 45 | Cindy's teacher is Alice. | ACTIVE"
            if interface == "MEMORY":
                if "Q1 [ACTIVE]" in prompt:
                    return "BIND | Q1 | Alice | 45"
                return "BIND | Q2 | Mary | Doc44"
            if interface == "RECALL":
                fid = next(line.split(" | ")[0] for line in prompt.splitlines()
                           if line.startswith("F") and "Alice's mother is Mary." in line)
                return "SELECT | " + fid
            if interface == "VERIFY":
                raw = prompt.split("Their already-read original sources:\n", 1)[1]
                assert "Alice's mother is Mary." in raw
                assert "Cindy's teacher is Alice." not in raw
                fid = next(line.split(" | ")[0] for line in prompt.splitlines()
                           if line.startswith("F") and "Alice's mother is Mary." in line)
                return fid + " | MATCH"
            if interface == "ANSWER":
                return json.dumps({"answer": "Mary", "source_refs": ["44", "Document 45"]})
            raise AssertionError(interface)

    store = SQLiteEventStore()
    client = Client()
    result = asyncio.run(V5Runner(client, config=RunnerConfig(chunk_size=len(first), answer_format="json")).run(
        run_id="chain", question="Who is Cindy's teacher's mother?", context=first + second, plan=plan, store=store,
    ))
    assert result["state"]["bindings"] == {"?teacher": "Alice", "?mother": "Mary"}
    assert len(result["state"]["working_memory"]["facts"]) == 2
    assert len(result["state"]["working_memory"]["links"]) == 1
    assert client.calls.count("VERIFY") == 1
    assert result["answer"]["source_refs"] == ["chain:c00000:D44", "chain:c00001:D45"]
    assert replay_events(store.list_runtime_events("chain")).export() == result["state"]


def test_bad_reference_triggers_repair_before_facts_are_applied():
    plan = QueryPlan(plan_id="film", queries=[{"id": "Q1", "template": "When was Film released?", "output": "?year"}])

    class Client:
        update_calls = 0

        async def complete(self, *, interface, messages, **kwargs):
            if interface == "UPDATE":
                self.update_calls += 1
                if self.update_calls == 1:
                    return "Q1 | Doc99 | Film was released in 2007. | ACTIVE"
                assert "Allowed original-source IDs (field 2):\nD44@C0" in messages[0]["content"]
                assert "Q1 | Doc99 | Film was released in 2007. | ACTIVE" in messages[0]["content"]
                return "Q1 | D44@C0 | Film was released in 2007. | ACTIVE"
            if interface == "MEMORY":
                return "BIND | Q1 | 2007 | D44"
            if interface == "ANSWER":
                return r"\boxed{2007}"
            raise AssertionError(interface)

    client, store = Client(), SQLiteEventStore()
    result = asyncio.run(V5Runner(client).run(run_id="repair", question="When was Film released?",
        context="Document 44:\nFilm\nFilm was released in 2007.", plan=plan, store=store))
    assert client.update_calls == 2
    assert result["state"]["bindings"] == {"?year": 2007}
    assert len(result["evidence_pack"]["facts"]) == 1
    assert sum(e.event_type == "AGENT_RESPONSE_INVALID" for e in store.list_runtime_events("repair")) == 1
