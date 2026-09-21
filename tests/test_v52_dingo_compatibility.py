"""V5.2 is additive to the latest V5.1 solitary-dingo baseline."""

import asyncio
import json

import pytest

from delaybind_core.archive import SentenceArchive, FutureSourceAccessError
from delaybind_core.cursor_v52 import TextReadCursor
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema import QueryPlan, parse_query_plan
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.token_budget import TokenCounter
from delaybind_core.v52_smoke import ScriptedSmokeClient, smoke_plan


class Characters:
    name = "original-characters"

    def encode(self, text, **kwargs):
        return list(text)

    def decode(self, tokens):
        raise AssertionError("original evidence must not pass through decode")


def test_numbered_documents_preserve_original_slices_titles_and_permissions():
    text = "Document 44:\nMary\nMary  was born in Suzhou.\n\nDocument 45:\nCindy\nCindy's teacher is Alice."
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "raw")
    cursor = TextReadCursor(text, archive, sample_id="s", counter=TokenCounter(Characters()), window_mode="fragment")
    window = cursor.next_anchored_window(25)
    assert window.source_refs[0] == "D44:H0:P1"
    for ref in ("D44:H0", "D44:S0", "D45:H0"):
        with pytest.raises(FutureSourceAccessError):
            archive.fetch_sentence(ref)
    pieces = [archive.fetch_sentence(r).text for r in window.source_refs]
    while not cursor.exhausted:
        window = cursor.next_anchored_window(25)
        pieces.extend(archive.fetch_sentence(r).text for r in window.source_refs)
    assert "".join(pieces) == text
    assert archive.fetch_sentence("D44:H0").text == "Document 44:\nMary\n"
    assert archive.fetch_sentence("D45:H0").text == "Document 45:\nCindy\n"
    assert archive.anchor("D45:H0").original_char_start == 0
    assert archive.anchor("D44:S0").original_char_start == len("Document 44:\nMary\n")
    assert all(r.source_ref.startswith("D44:") for r in archive.fetch_bounded_context("D44:S0", before=9, after=9))


@pytest.mark.parametrize("input_form", ["item", "question_context"])
def test_dingo_input_forms_actual_tokenizer_reader_client_and_auto_boxed(input_form):
    text = "Document 44:\nMary\nMary was born in Suzhou.\nDocument 45:\nAlice\nAlice's mother is Mary.\nDocument 46:\nCindy\nCindy's teacher is Alice."
    question = "Where was Cindy's teacher's mother born?"
    high, low = ScriptedSmokeClient(), ScriptedSmokeClient()
    cfg = RunnerConfig(protocol_version="v5.2", chunk_size=90, memory_token_budget=30000,
                       max_review_input_tokens=50000, max_answer_input_tokens=50000,
                       max_input_tokens=50000, max_context_tokens=60000)
    kwargs = ({"item": {"_id": "long", "input": question, "context": text}}
              if input_form == "item" else {"question": question, "context": text})
    result = asyncio.run(V5Runner(high, reader_client=low, config=cfg, tokenizer=Characters()).run(
        run_id="long", store=SQLiteEventStore(), **kwargs))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["raw_answer"] == "\\boxed{Suzhou}"
    assert result["plan_format"] == "subqueries"
    assert set(i for i, _ in high.calls) <= {"PLAN", "MEMORY", "ANSWER"}
    assert set(i for i, _ in low.calls) <= {"UPDATE", "RECALL"}
    assert result["v52_metrics"]["transaction_replay_consistency"] == 1
    assert {r["source_ref"] for r in result["evidence_pack"]["raw_evidence"]} == {"D44:S0", "D45:S0", "D46:S0"}


def test_version_dispatch_preserves_v51_and_rejects_cross_version_plans():
    assert RunnerConfig().protocol_version == "v5.1"
    legacy = parse_query_plan({"plan_id": "legacy", "queries": [{"id": "Q1", "query": "Who?", "binds": "?who"}]})
    assert isinstance(legacy, QueryPlan)
    assert isinstance(parse_query_plan(smoke_plan().model_dump()), QueryPlanV3)
    with pytest.raises(ValueError, match="PLAN_PROTOCOL_MISMATCH"):
        asyncio.run(V5Runner(ScriptedSmokeClient()).run(run_id="wrong", question="q", context="x",
                    store=SQLiteEventStore(), plan=smoke_plan()))


def test_character_and_token_memory_budgets_are_distinct_and_explicit():
    from delaybind_core.v52_smoke import run_smoke
    from dataclasses import replace
    cfg = RunnerConfig(protocol_version="v5.2", chunk_size=28, memory_char_budget=1)
    limited = asyncio.run(run_smoke(config=cfg))
    assert limited["status"] == "RESOURCE_LIMIT"
    assert "FINAL_RAW_MEMORY_BUDGET" in limited["reason_codes"]
    token_mode = asyncio.run(run_smoke(config=replace(cfg, memory_token_budget=16000)))
    assert token_mode["status"] == "ANSWERED"


def test_repeated_failed_calls_and_cache_hits_remain_auditable_without_double_billing(monkeypatch):
    from delaybind_core.api import APIConfig, OpenAICompatibleClient
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(APIConfig(base_url="http://unused", api_key="x", model="mock", max_retries=0), store=store)
    def fail(_payload):
        raise RuntimeError("offline")
    monkeypatch.setattr(client, "_call_sync", fail)
    kwargs = dict(run_id="billing", interface="UPDATE", messages=[{"role": "user", "content": "same"}], agent_role="LOW")
    for _ in range(2):
        with pytest.raises(RuntimeError):
            asyncio.run(client.complete(**kwargs))
    monkeypatch.setattr(client, "_call_sync", lambda p: ({"choices": [{"message": {"content": "ok"}}],
                  "usage": {"prompt_tokens": 12, "completion_tokens": 3}}, 10))
    for _ in range(4):
        assert asyncio.run(client.complete(**kwargs)) == "ok"
    rows = store.list_model_calls("billing")
    assert [c.attempt for c in rows] == [1, 2, 3, 0, -1, -2]
    summary = store.model_call_summary("billing")
    assert summary["failed_attempts"] == 2 and summary["cache_hits"] == 3
    assert summary["input_tokens"] == 12 and summary["output_tokens"] == 3
    assert summary["interface_usage"]["LOW:UPDATE"]["input_tokens"] == 12
