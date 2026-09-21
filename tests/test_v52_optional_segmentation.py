import asyncio
from dataclasses import replace

import pytest

from delaybind_core.archive import RawArchive, SentenceArchive, FutureSourceAccessError
from delaybind_core.cli import build_parser, _with_sentence_splitting
from delaybind_core.cursor import TextReadCursor, ReadCursor
from delaybind_core.cursor_legacy_v52 import LegacyReadCursor
from delaybind_core.data import build_manifest, canonicalize_record
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.source_refs_v52 import SentenceRefResolver
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.v52_smoke import ScriptedSmokeClient, run_smoke, smoke_plan


class Characters:
    name = "lossless-character-tokenizer"

    def encode(self, text):
        return list(text)

    def decode(self, tokens):
        return "".join(tokens)


@pytest.mark.parametrize("tokenizer", [None, Characters()])
@pytest.mark.parametrize("budget", [5, 29, 500])
def test_disabled_windows_match_actual_v51_with_split_headers(tokenizer, budget):
    text = "前言。\nDocument 18:\nFilm\nFirst sentence.Second sentence!\nDocument 44:\nActor\n出生地是杭州。"
    store = SQLiteEventStore()
    old = TextReadCursor(text, RawArchive(store, "old"), sample_id="s", tokenizer=tokenizer)
    archive = SentenceArchive(store, "new")
    new = LegacyReadCursor(archive, text=text, sample_id="s", tokenizer=tokenizer)
    pieces = []
    while not old.exhausted:
        old_window, new_window = old.next_window(budget), new.next_anchored_window(budget)
        raw = [archive.fetch_sentence(ref) for ref in new_window.source_refs]
        assert [r.text for r in raw] == [e.text for e in old_window.entries]
        assert all(r.kind == "chunk" and r.complete for r in raw)
        assert SentenceRefResolver(new_window.source_refs).resolve(list(new_window.source_refs))
        pieces.extend(r.text for r in raw)
        assert new.window_index == old.window_index
    assert new.exhausted and "".join(pieces) == text


def test_legacy_cursor_resume_preserves_partial_header_and_read_gate():
    text = "Document 18:\nAlpha\nA. B.\nDocument 44:\nBeta\nC. D."
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    cursor = LegacyReadCursor(archive, text=text, sample_id="s")
    cursor.next_anchored_window(8)  # Stops in Document header, before number.
    state, watermark = cursor.state(), archive.watermark
    expected = []
    while not cursor.exhausted:
        expected.append(cursor.next_anchored_window(8))
    restored = SentenceArchive(store, "r", watermark=watermark)
    with pytest.raises(FutureSourceAccessError):
        restored.fetch_sentence("D18@C1")
    resumed = LegacyReadCursor(restored, text=text, sample_id="s", state=state)
    actual = []
    while not resumed.exhausted:
        actual.append(resumed.next_anchored_window(8))
    assert actual == expected
    assert resumed.state() == cursor.state()
    assert all(r.source_ref.startswith("D18@") for r in restored.fetch_bounded_context("D18@C1", before=4, after=4))
    with pytest.raises(ValueError, match="CURSOR_SOURCE_OR_MODE_CHANGED"):
        LegacyReadCursor(restored, text=text + "changed", sample_id="s", state=state)


def test_no_sentence_operation_for_raw_text_and_chunk_evidence_end_to_end(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("sentence segmentation must not run")
    monkeypatch.setattr("delaybind_core.sentence_index.text_manifest", forbidden)
    monkeypatch.setattr("delaybind_core.sentence_index.sentence_spans", forbidden)
    text = ("Document 18:\nCindy\nCindy's teacher is Alice.\n"
            "Document 44:\nAlice\nAlice's mother is Mary.\n"
            "Document 45:\nMary\nMary was born in Suzhou.")
    store, client = SQLiteEventStore(), ScriptedSmokeClient()
    cfg = RunnerConfig(protocol_version="v5.2", sentence_splitting=False, chunk_size=1000)
    runner = V5Runner(client, config=cfg)
    result = asyncio.run(runner.run(run_id="chunks", question="q", context=text, store=store, plan=smoke_plan()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Suzhou"
    assert result["v52_metrics"]["transaction_replay_consistency"] == 1
    raw = result["evidence_pack"]["raw_evidence"]
    assert {r["source_ref"] for r in raw} == {"D18@C0", "D44@C0", "D45@C0"}
    assert all(r["kind"] == "chunk" for r in raw)
    assert result["state"]["cursor_state"]["budget_unit"] == "characters"
    assert result["config"]["config"]["sentence_splitting"] is False
    count = len(client.calls)
    resumed = asyncio.run(runner.run(run_id="chunks", question="q", context=text, store=store, plan=smoke_plan()))
    assert resumed["state"] == result["state"] and len(client.calls) == count
    # Reject switching modes on the same run before even trying segmentation.
    with pytest.raises(ValueError, match="CURSOR_SOURCE_OR_MODE_CHANGED"):
        LegacyReadCursor(SentenceArchive(store, "chunks"), text=text, sample_id="s",
                         state={"segmentation_version": "sentence"})


def test_switching_run_mode_is_rejected():
    store, client = SQLiteEventStore(), ScriptedSmokeClient()
    cfg = RunnerConfig(protocol_version="v5.2", sentence_splitting=False, chunk_size=1000)
    asyncio.run(run_smoke(store=store, client=client, config=cfg))
    count = len(client.calls)
    with pytest.raises(ValueError, match="RESUME_CONFIGURATION_CHANGED"):
        asyncio.run(run_smoke(store=store, client=client, config=replace(cfg, sentence_splitting=True)))
    assert len(client.calls) == count


def test_explicit_manifest_preserves_v51_entry_windows_without_resplitting():
    sample = canonicalize_record({"id": "s", "question": "q", "context": [
        ["A", ["One. Two.No additional splitting!", "More words here."]], ["B", ["Another original entry."]]]})
    manifest = build_manifest(sample)
    store = SQLiteEventStore()
    old = ReadCursor(manifest, RawArchive(store, "old"))
    archive = SentenceArchive(store, "new")
    new = LegacyReadCursor(archive, manifest=manifest)
    while not old.exhausted:
        a, b = old.next_window(5), new.next_anchored_window(5)
        assert [e.text for e in a.entries] == [archive.fetch_sentence(ref).text for ref in b.source_refs]
    assert new.state()["budget_unit"] == "words"


def test_lossy_legacy_decode_is_not_registered_as_original_evidence():
    class Lossy(Characters):
        def decode(self, tokens):
            return "".join(tokens).upper()
    archive = SentenceArchive(SQLiteEventStore(), "r")
    cursor = LegacyReadCursor(archive, text="raw text", sample_id="s", tokenizer=Lossy())
    with pytest.raises(ValueError, match="LEGACY_CHUNK_NOT_VERBATIM"):
        cursor.next_anchored_window(100)
    assert archive.watermark == -1


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("false", False), ("off", False), ("true", True)])
def test_config_flag_parsing(value, expected):
    assert RunnerConfig.from_mapping({"sentence_splitting": value}).sentence_splitting is expected


@pytest.mark.parametrize("value", ["maybe", None, 2])
def test_invalid_config_flag_is_not_silently_enabled(value):
    with pytest.raises(ValueError, match="sentence_splitting"):
        RunnerConfig.from_mapping({"sentence_splitting": value})


@pytest.mark.parametrize("command", ["run", "experiment"])
def test_cli_switches_override_config_without_changing_original(command):
    parser = build_parser()
    assert parser.parse_args([command, "--config", "config.json"]).sentence_splitting is None
    assert parser.parse_args([command, "--config", "config.json", "--sentence-splitting"]).sentence_splitting is True
    assert parser.parse_args([command, "--config", "config.json", "--no-sentence-splitting"]).sentence_splitting is False
    for config in ({"input": "x", "sentence_splitting": True},
                   {"input": "x", "runner": {"protocol_version": "v5.2", "sentence_splitting": True}}):
        updated = _with_sentence_splitting(config, False)
        assert ExperimentConfig.from_mapping(updated).runner.sentence_splitting is False
        assert ExperimentConfig.from_mapping(config).runner.sentence_splitting is True
        assert _with_sentence_splitting(config, None) is config
