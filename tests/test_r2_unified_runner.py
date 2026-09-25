"""Offline request, transport-integrity and budget checks for unified MEMORY."""

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from delaybind_core.api import APIConfig, ModelAPIError, ModelCompletion, OpenAICompatibleClient
from delaybind_core.evaluation import ExperimentConfig
from delaybind_core.fixture_wire_r2 import smoke_plan
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema_r2 import EvidencePlanR2, member_plan
from delaybind_core.storage import SQLiteEventStore


class CharacterTokenizer:
    name_or_path = "unified-test-character-tokenizer"

    def encode(self, text):
        return list(text.encode())

    def decode(self, tokens):
        return bytes(tokens).decode()


class UnifiedChainClient:
    def __init__(self, *, memory_finish=None, evidence=None, unknown=False):
        self.calls = []
        self.memory_finish = memory_finish
        self.evidence = evidence or (
            "Q1 | Cindy's teacher is Bob.\n"
            "Q2 | Bob's mother is Mary.\n"
            "Q3 | Mary was born in Suzhou."
        )
        self.unknown = unknown

    async def complete(self, *, interface, messages, **kwargs):
        self.calls.append((interface, messages, kwargs))
        if interface == "UPDATE":
            return self.evidence
        if interface in {"MEMORY", "MEMORY_REPAIR"}:
            body = messages[-1]["content"]
            query = re.search(r"Current query:\n(Q\d+) \| ([^\n]+)", body)
            assert query, body
            qid = query[1]
            if self.unknown:
                response = f"{qid} | NONE | UNKNOWN"
            else:
                supported = {"Q1": ("Cindy's teacher is Bob.", "Bob"),
                             "Q2": ("Bob's mother is Mary.", "Mary"),
                             "Q3": ("Mary was born in Suzhou.", "Suzhou")}
                fact, value = supported[qid]
                fid = re.search(r"(F\d+) \| " + re.escape(fact), body)[1]
                response = f"{qid} | {fid} | {value}"
            return (ModelCompletion(response, finish_reason=self.memory_finish)
                    if self.memory_finish is not None else response)
        if interface == "ANSWER":
            return r"\boxed{UNKNOWN}" if self.unknown else r"\boxed{Suzhou}"
        raise AssertionError(f"Unexpected interface {interface}")


def config(**updates):
    return replace(RunnerConfig(sentence_splitting=False, memory_interface="unified_evidence_v1",
                                update_input_mode="plain_token_chunks", chunk_size=20000), **updates)


def run(client, *, cfg=None, plan=None, store=None):
    return asyncio.run(V5Runner(client, config=cfg or config(), tokenizer=CharacterTokenizer()).run(
        run_id="unified-chain", question="GLOBAL_QUESTION_SENTINEL", context="Document 1: family notes.",
        store=store or SQLiteEventStore(), plan=plan or member_plan(smoke_plan())))


def test_three_hops_direct_memory_loads_dormant_evidence_and_resumes_without_calls():
    client, store = UnifiedChainClient(), SQLiteEventStore()
    result = run(client, store=store)
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Suzhou"
    assert result["interface_calls"] == {"UPDATE": 1, "MEMORY": 3, "ANSWER": 1}
    calls = [messages for interface, messages, _ in client.calls if interface == "MEMORY"]
    assert "Who is Bob's mother?" in calls[1][-1]["content"]
    assert "Where was Mary born?" in calls[2][-1]["content"]
    for messages in calls:
        body = "\n".join(message["content"] for message in messages)
        assert "GLOBAL_QUESTION_SENTINEL" not in body
        for forbidden in ("Existing binding:", "Mode:", "Old facts", "New facts", "Trigger:"):
            assert forbidden not in body
    metrics = result["r2_metrics"]
    assert metrics["recall_model_calls"] == metrics["unknown_result_count"] == 0
    assert metrics["supported_member_count"] == metrics["binding_changed_count"] == 3
    assert metrics["memory_input_tokens"] > 0 and metrics["memory_output_tokens"] > 0
    assert metrics["transaction_replay_consistency"] == 1.0
    requests = [m for m in result["context_manifests"]
                if m.get("kind") == "MODEL_REQUEST" and m["interface"] == "MEMORY"]
    assert all(m["memory_interface_version"] == "query-facts-result-v1" for m in requests)
    assert all(m["evidence_fact_count"] == m["rendered_fact_count"] == 1 for m in requests)
    assert any("DEFERRED" in m["evidence_origins"] for m in requests)
    before = len(client.calls)
    resumed = run(client, store=store)
    assert resumed["state"] == result["state"] and len(client.calls) == before
    with pytest.raises(ValueError, match="RESUME_MEMORY_INTERFACE_CHANGED"):
        run(client, cfg=replace(config(), memory_interface="legacy_bind_rebind_v1"), store=store)


def test_candidate_evidence_does_not_use_resident_memory_budget():
    # A large unaccepted candidate must be judged before the empty final
    # effective working memory is checked against its much smaller budget.
    evidence = "Q1 | A contextual fact with details " + "unrelated " * 1000
    client = UnifiedChainClient(evidence=evidence, unknown=True)
    plan = EvidencePlanR2(plan_id="budget", queries=[dict(id="Q1", template="Who teaches Cindy?", output="?teacher")])
    result = run(client, cfg=config(memory_char_budget=1000), plan=plan)
    assert result["status"] == "INSUFFICIENT", result["reason_codes"]
    assert result["interface_calls"]["MEMORY"] == 1
    body = next(messages[-1]["content"] for interface, messages, _ in client.calls if interface == "MEMORY")
    assert evidence.split(" | ", 1)[1].strip() in body
    assert result["r2_metrics"]["unknown_result_count"] == 1
    assert len(result["state"]["facts"]) == 1


@pytest.mark.parametrize("budget", [{"max_review_input_tokens": 1}, {"max_context_tokens": 10000}])
def test_oversized_memory_is_resource_error_and_never_unknown(budget):
    evidence = "Q1 | A contextual fact with details " + "unrelated " * 1300
    client = UnifiedChainClient(evidence=evidence, unknown=True)
    plan = EvidencePlanR2(plan_id="budget", queries=[dict(id="Q1", template="Who teaches Cindy?", output="?teacher")])
    result = run(client, cfg=config(**budget), plan=plan)
    assert result["status"] == "RESOURCE_LIMIT", result["reason_codes"]
    assert "UNIFIED_MEMORY_INPUT_BUDGET" in result["reason_codes"]
    assert result["interface_calls"].get("MEMORY", 0) == 0
    assert result["r2_metrics"]["unknown_result_count"] == 0
    assert not result["state"]["binding_store"]


def test_truncated_legal_prefix_is_never_published_or_repaired_as_unknown():
    client = UnifiedChainClient(memory_finish="length")
    result = run(client)
    assert result["status"] == "RUNTIME_ERROR", result["reason_codes"]
    assert any("INCOMPLETE_MODEL_RESPONSE:length" in code for code in result["reason_codes"])
    assert result["interface_calls"].get("MEMORY_REPAIR", 0) == 0
    assert result["interface_calls"].get("ANSWER", 0) == 0
    assert not result["state"]["binding_store"]
    assert result["r2_metrics"]["unknown_result_count"] == 0


def test_provider_truncation_fails_runner_and_counts_attempt_tokens(monkeypatch):
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(APIConfig(base_url="http://unused", api_key="x", model="mock", max_retries=0), store=store)

    def respond(payload):
        body = payload["messages"][-1]["content"]
        memory = "Protocol: query-facts-result-v1" in body
        return {"choices": [{"message": {"content": ("Q1 | F1 | Bob" if memory else
                                                        "Q1 | Cindy's teacher is Bob.")},
                             "finish_reason": "length" if memory else "stop"}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 12}}, 1.0

    monkeypatch.setattr(client, "_call_sync", respond)
    result = run(client, store=store)
    assert result["status"] == "RUNTIME_ERROR", result["reason_codes"]
    assert not result["state"]["binding_store"]
    assert result["r2_metrics"]["unknown_result_count"] == 0
    assert result["r2_metrics"]["memory_input_tokens"] == 120
    assert result["r2_metrics"]["memory_output_tokens"] == 12
    assert result["r2_metrics"]["memory_token_usage_source"] == "provider"


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", None])
def test_api_rejects_incomplete_unified_response_and_retains_raw_audit(monkeypatch, finish_reason):
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(APIConfig(base_url="http://unused", api_key="x", model="mock", max_retries=0), store=store)
    response = {"choices": [{"message": {"content": "Q1 | F1 | Bob"}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8}}
    monkeypatch.setattr(client, "_call_sync", lambda payload: (response, 1.0))
    with pytest.raises(ModelAPIError, match="INCOMPLETE_MODEL_RESPONSE"):
        asyncio.run(client.complete(run_id="incomplete", interface="MEMORY", messages=[],
                                    local_metadata={"memory_interface_version": "query-facts-result-v1"}))
    [call] = store.list_model_calls("incomplete")
    assert call.raw_response == response and call.parsed_output is None
    assert call.output_tokens == 8 and call.error


def test_api_complete_response_usage_and_cache_keep_completion_status(monkeypatch):
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(APIConfig(base_url="http://unused", api_key="x", model="mock", max_retries=0), store=store)
    response = {"choices": [{"message": {"content": "Q1 | F1 | Bob"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8}}
    monkeypatch.setattr(client, "_call_sync", lambda payload: (response, 1.0))
    kwargs = dict(run_id="complete", interface="MEMORY", messages=[],
                  local_metadata={"memory_interface_version": "query-facts-result-v1"})
    first = asyncio.run(client.complete(**kwargs))
    second = asyncio.run(client.complete(**kwargs))
    assert first == second == "Q1 | F1 | Bob"
    assert first.finish_reason == second.finish_reason == "stop"
    assert first.input_tokens == second.input_tokens == 20
    assert first.output_tokens == second.output_tokens == 8
    assert store.list_model_calls("complete")[-1].cache_hit


def test_new_presets_change_only_interface_and_experiment_identity():
    root = Path(__file__).parents[1] / "configs"
    baseline = json.loads((root / "v52_r2_full128_member_graph_plain_chunks_optional_repair_c4.json").read_text())
    for name, count in [("v52_r2_full128_unified_memory_c4.json", 128), ("v52_r2_smoke8_unified_memory.json", 8)]:
        preset = json.loads((root / name).read_text())
        assert preset["api"] == baseline["api"]
        assert preset["runner"] == {**baseline["runner"], "memory_interface": "unified_evidence_v1"}
        assert preset["experiment_id"] != baseline["experiment_id"]
        assert preset["output_dir"] != baseline["output_dir"]
        assert preset["sample_count"] == count and preset["max_concurrency"] == 4
        assert preset["orders"] == baseline["orders"]
        parsed = ExperimentConfig.from_mapping(preset)
        assert parsed.runner.protocol_mapping()["memory_interface"] == "unified_evidence_v1"
    assert "memory_interface" not in RunnerConfig().protocol_mapping()
    with pytest.raises(ValueError, match="requires fact-only"):
        RunnerConfig(memory_interface="unified_evidence_v1")
    with pytest.raises(ValueError, match="invalid memory_interface"):
        RunnerConfig(memory_interface="guess-protocol")
