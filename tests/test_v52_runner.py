import asyncio
import json
from dataclasses import replace

import pytest

from delaybind_core.agents_v52 import LowLevelAgentV52
from delaybind_core.data import build_manifest, canonicalize_record
from delaybind_core.replay import replay_events
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.v52_smoke import ScriptedSmokeClient, run_smoke, read_fixture_request, fixture_response
from delaybind_core.text_protocol_v52 import parse_memory


def config(**kwargs):
    return RunnerConfig(protocol_version="v5.2", plan_format="subqueries", chunk_size=28, **kwargs)


def test_t02_t25_t28_reverse_chain_corrects_extraction_from_raw_and_no_verify():
    store, client = SQLiteEventStore(), ScriptedSmokeClient(corrupt_extraction=True)
    result = asyncio.run(run_smoke(store=store, client=client))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Suzhou"
    assert result["interface_calls"].get("VERIFY", 0) == 0
    assert result["interface_calls"]["RECALL"] == 2
    assert result["v52_metrics"]["raw_coverage_of_binding_support"] == 1.0
    assert result["v52_metrics"]["recall_scan_completion"] == 1.0
    assert replay_events(store.list_runtime_events("v52-smoke")).export() == result["state"]
    final = next(data for interface, data in client.calls if interface == "ANSWER")
    assert set(final) == {"question", "working_memory", "answer_contract"}
    assert len(final["working_memory"]["raw_evidence"]) == 3
    assert any("Shanghai" in f["text"] for f in result["state"]["facts"].values())
    assert any(f["supersedes_fact_id"] for f in result["state"]["facts"].values())


def test_t03_same_window_registration_is_order_independent():
    class ReversedUpdate(ScriptedSmokeClient):
        async def complete(self, **kwargs):
            raw = await super().complete(**kwargs)
            if kwargs["interface"] == "UPDATE":
                return "\n".join(reversed(raw.splitlines()))
            return raw

    cfg = replace(config(), chunk_size=1000)
    first = asyncio.run(run_smoke(client=ScriptedSmokeClient(), config=cfg))
    second = asyncio.run(run_smoke(client=ReversedUpdate(), config=cfg))
    assert first["state"]["bindings"] == second["state"]["bindings"]
    assert first["status"] == second["status"] == "ANSWERED"
    assert first["evidence_pack"] == second["evidence_pack"]


def test_t15_recall_parse_failure_preserves_scan_barrier_and_partial_answer():
    class FailedRecall(ScriptedSmokeClient):
        async def complete(self, **kwargs):
            if kwargs["interface"] == "RECALL":
                return "SELECT not-in-this-batch"
            return await super().complete(**kwargs)

    result = asyncio.run(run_smoke(client=FailedRecall()))
    assert result["status"] == "RUNTIME_ERROR"
    assert "SCAN_INCOMPLETE" in result["reason_codes"]
    assert result["state"]["executions"]["Q2"]["review_barrier"]
    assert result["state"]["executions"]["Q2"]["current_binding_id"] is None
    assert result["interface_calls"]["ANSWER"] == 1


@pytest.mark.parametrize("answer_format", ["boxed", "text"])
def test_text_and_boxed_do_not_forge_model_citations(answer_format):
    result = asyncio.run(run_smoke(config=config(answer_format=answer_format)))
    assert result["answer"]["answer"] == "Suzhou"
    assert result["answer"]["source_refs"] == []
    assert len(result["answer_context_source_refs"]) == 3


def test_t26_empty_memory_still_calls_answer_and_t27_budget_failure_explicit():
    class Empty(ScriptedSmokeClient):
        async def complete(self, **kwargs):
            if kwargs["interface"] == "UPDATE":
                return "NONE"
            return await super().complete(**kwargs)

    empty = asyncio.run(run_smoke(client=Empty()))
    assert empty["interface_calls"]["ANSWER"] == 1
    assert empty["answer"]["answer"] is None
    assert empty["status"] == "INSUFFICIENT"
    limited = asyncio.run(run_smoke(config=config(max_answer_input_tokens=10)))
    assert limited["status"] == "RESOURCE_LIMIT"
    assert "FINAL_RAW_MEMORY_BUDGET" in limited["reason_codes"]
    assert limited["answer"] is None
    assert limited["evidence_pack"]["raw_evidence"]  # No fact-only fallback.


def test_answer_reserve_stops_reading_without_hiding_resource_error():
    result = asyncio.run(run_smoke(config=config(max_model_calls=4)))
    assert result["status"] == "RESOURCE_LIMIT"
    assert result["logical_model_calls"] == 4
    assert result["interface_calls"]["ANSWER"] == 1
    assert not result["state"]["scope_closed"]


def test_completed_run_resume_is_idempotent():
    store, client = SQLiteEventStore(), ScriptedSmokeClient()
    first = asyncio.run(run_smoke(store=store, client=client))
    count = len(client.calls)
    second = asyncio.run(run_smoke(store=store, client=client))
    assert first["state"] == second["state"]
    assert len(client.calls) == count


def test_role_capability_refuses_low_verify():
    with pytest.raises(PermissionError, match="INTERFACE_NOT_AUTHORIZED"):
        asyncio.run(LowLevelAgentV52(ScriptedSmokeClient()).call("VERIFY"))


def test_no_callback_and_no_defer_are_distinct_ablations():
    callback_off = asyncio.run(run_smoke(config=config(enable_defer_callback=False)))
    defer_off = asyncio.run(run_smoke(config=config(defer_unbound=False)))
    assert len(callback_off["state"]["facts"]) == 3
    assert len(defer_off["state"]["facts"]) == 1
    assert callback_off["interface_calls"].get("RECALL", 0) == 0


def test_t14_all_recall_batches_scanned_then_last_review_conflict_blocks_binding():
    sample = canonicalize_record({"id": "s", "question": "Where was Cindy's teacher born?", "context": [
        ["Cities", ["Alice was born in Suzhou.", "Alice was born in Shanghai."]],
        ["Cindy", ["Cindy's teacher is Alice."]]]})
    plan = QueryPlanV3(plan_id="p", queries=[
        {"id": "Q1", "template": "Who teaches Cindy?", "output": "?teacher"},
        {"id": "Q2", "template": "Where was ?teacher born?", "output": "?city", "inputs": {"?teacher": "Q1"}}])

    class ConflictClient(ScriptedSmokeClient):
        async def complete(self, *, interface, messages, **kwargs):
            data = read_fixture_request(messages)
            if interface == "UPDATE":
                return fixture_response({"facts": [{"query_ids": ["Q1" if "teacher" in raw["text"] else "Q2"],
                                              "source_refs": [raw["source_ref"]], "text": raw["text"]}
                                             for raw in data["window_sources"] if raw["kind"] == "sentence"], "hints": []})
            if interface == "MEMORY" and "Q2" in data["input_signatures"]:
                self.calls.append((interface, data))
                pending = data["pending_uses"]
                nodes = {f["fact_id"]: f for f in data["working_memory"]["navigation"]["facts"]}
                assert sum(i == "RECALL" for i, _ in self.calls) == 2
                ops = [{"op": "ASSESS", "query_id": "Q2", "fact_id": p["fact_id"],
                        "verdict": "CONFLICT" if "Shanghai" in nodes[p["fact_id"]]["text"] else "ACCEPT",
                        "checked_refs": nodes[p["fact_id"]]["source_refs"], "reason_code": "SOURCE_CONFLICT"}
                       for p in pending]
                return fixture_response({"schema_version": "memory-v5.2", "context_id": data["context_id"], "operations": ops})
            return await super().complete(interface=interface, messages=messages, **kwargs)

    client, store = ConflictClient(), SQLiteEventStore()
    result = asyncio.run(V5Runner(client, config=replace(config(candidate_batch_size=1), chunk_size=100)).run(
        run_id="conflict", sample=sample, manifest=build_manifest(sample), store=store, plan=plan))
    assert result["state"]["executions"]["Q2"]["current_binding_id"] is None
    calls = [d for i, d in client.calls if i == "MEMORY" and d["pending_uses"] and "Q2" in d["input_signatures"]]
    assert len(calls) == 2
    assert calls[0]["bindable_query_ids"] == []
    assert calls[1]["bindable_query_ids"] == ["Q2"]
    assert any(n.get("use_status") == "CONFLICT" for n in result["evidence_pack"]["unresolved_or_conflicts"])


def test_t08_t16_hold_reopens_when_future_neighbor_is_read():
    sample = canonicalize_record({"id": "s", "question": "Where was Mary born?", "context": [
        ["Mary", ["She was born there.", "She refers to Mary; there means Suzhou."]]]})
    plan = QueryPlanV3(plan_id="p", queries=[{"id": "Q1", "template": "Where was Mary born?", "output": "?city"}])

    class HoldClient(ScriptedSmokeClient):
        async def complete(self, *, interface, messages, **kwargs):
            data = read_fixture_request(messages)
            if interface == "UPDATE":
                facts = [{"query_ids": ["Q1"], "source_refs": [r["source_ref"]], "text": "She was born there."}
                         for r in data["window_sources"] if r["source_ref"] == "D0:S0"]
                return fixture_response({"facts": facts, "hints": []})
            if interface == "MEMORY":
                self.calls.append((interface, data))
                ops = []
                raw = {r["source_ref"]: r["text"] for r in data["working_memory"]["raw_evidence"]}
                for p in data["pending_uses"]:
                    op = {"op": "ASSESS", "query_id": "Q1", "fact_id": p["fact_id"],
                          "checked_refs": ["D0:S0"], "reason_code": "PRONOUN_CONTEXT"}
                    if "D0:S1" in raw:
                        assert "Suzhou" in raw["D0:S1"]
                        op.update(verdict="ACCEPT", checked_refs=["D0:S0", "D0:S1"], correction={
                            "local_id": "N1", "text": "Mary was born in Suzhou.", "source_refs": ["D0:S0", "D0:S1"]})
                        ops.extend([op, {"op": "BIND", "query_id": "Q1", "value": "Suzhou", "kind": "DIRECT", "support_fact_ids": ["N1"]}])
                    else:
                        op.update(verdict="HOLD", context_request={"anchor": "D0:S0", "before": 1, "after": 1})
                        ops.append(op)
                return fixture_response({"schema_version": "memory-v5.2", "context_id": data["context_id"], "operations": ops})
            return await super().complete(interface=interface, messages=messages, **kwargs)

    result = asyncio.run(V5Runner(HoldClient(), config=replace(config(), chunk_size=20)).run(
        run_id="hold", sample=sample, manifest=build_manifest(sample), store=SQLiteEventStore(), plan=plan))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["state"]["bindings"] == {"?city": "Suzhou"}
    assert result["state"]["context_expansions"] >= 1
    assert result["state"]["binding_store"]["B1"]["source_refs"] == ["D0:S0", "D0:S1"]


def test_memory_repair_uses_same_context_and_rolls_back_first_attempt():
    class RepairClient(ScriptedSmokeClient):
        failed_context = None

        async def complete(self, *, interface, messages, **kwargs):
            data = read_fixture_request(messages)
            if interface == "MEMORY" and self.failed_context is None and data["pending_uses"]:
                self.failed_context = data["context_id"]
                value = parse_memory(await super().complete(interface=interface, messages=messages, **kwargs), data["context_id"]).model_dump()
                value["operations"][-1]["support_fact_ids"] = ["F_not_visible"]
                return fixture_response(value)
            if interface == "MEMORY_REPAIR":
                assert data["context_id"] == self.failed_context
                from delaybind_core.agent_prompts_v52 import messages as build_messages
                return await super().complete(interface="MEMORY", messages=build_messages("MEMORY", data["original_memory_request"]), **kwargs)
            return await super().complete(interface=interface, messages=messages, **kwargs)

    result = asyncio.run(run_smoke(client=RepairClient()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["interface_calls"]["MEMORY_REPAIR"] == 1
    assert len(result["state"]["binding_store"]) == 3


def test_evaluation_v52_records_metrics_and_resource_failures(tmp_path):
    from delaybind_core.evaluation import ExperimentConfig, run_experiment
    dataset = tmp_path / "data.json"
    dataset.write_text(json.dumps([{"id": "smoke", "question": "Where was Cindy's teacher's mother born?",
        "answer": "Suzhou", "supporting_facts": [["Cindy", 0], ["Alice", 0], ["Mary", 0]], "context": [
            ["Cindy", ["Cindy's teacher is Alice."]], ["Alice", ["Alice's mother is Mary."]], ["Mary", ["Mary was born in Suzhou."]]]}]))
    conf = ExperimentConfig.from_mapping({"input": str(dataset), "output_dir": str(tmp_path / "output"),
        "methods": ["v52_predicted"], "orders": ["reverse"], "sample_count": 1,
        "runner": {"protocol_version": "v5.2", "chunk_size": 28}})
    asyncio.run(run_experiment(conf, client_factory=lambda store: ScriptedSmokeClient()))
    row = json.loads((tmp_path / "output" / "results.jsonl").read_text().strip())
    assert row["answer_exact"] == 1.0 and row["predicted_source_refs"] == []
    assert row["runtime_status"] == "ANSWERED" and row["run_success"]
    assert row["plan_relation_recall"] is None
    bad = replace(conf, output_dir=str(tmp_path / "limited"), runner=replace(conf.runner, max_answer_input_tokens=1))
    asyncio.run(run_experiment(bad, client_factory=lambda store: ScriptedSmokeClient()))
    row = json.loads((tmp_path / "limited" / "results.jsonl").read_text().strip())
    assert row["status"] == "ERROR" and row["runtime_status"] == "RESOURCE_LIMIT"
    assert not row["run_success"]


def test_cli_v52_dispatch_and_raw_token_audit(tmp_path, monkeypatch):
    from delaybind_core import cli
    from delaybind_core.api import APIConfig
    dataset = tmp_path / "data.json"
    dataset.write_text(json.dumps([{"id": "smoke", "question": "Where was Cindy's teacher's mother born?", "context": [
        ["Cindy", ["Cindy's teacher is Alice."]], ["Alice", ["Alice's mother is Mary."]], ["Mary", ["Mary was born in Suzhou."]]]}]))
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"input": str(dataset), "db": str(tmp_path / "new" / "state.sqlite"),
        "protocol_version": "v5.2", "chunk_size": 28, "order": "reverse"}))
    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "_api_config", lambda *args, **kwargs: APIConfig(base_url="http://unused", api_key="fixture", model="fixture"))
    monkeypatch.setattr(cli, "OpenAICompatibleClient", lambda *args, **kwargs: ScriptedSmokeClient())
    result = cli._run_from_config(config_path)
    assert result["state"]["bindings"]["?city"] == "Suzhou"
    calls = [c for c in result["context_manifests"] if c.get("kind") == "MODEL_REQUEST"]
    assert all(c["raw_input_tokens"] > 0 for c in calls if c["interface"] == "UPDATE")
    assert all(c["raw_input_tokens"] == 0 for c in calls if c["interface"] == "RECALL")
    assert all(c["visible_source_refs"] for c in calls if c["interface"] == "MEMORY")


def test_v52_does_not_relabel_a_historical_run():
    from delaybind_core.schema import RuntimeEvent
    store = SQLiteEventStore()
    store.append_runtime_event(RuntimeEvent(run_id="v52-smoke", event_id="old", event_type="PLAN_CREATED"))
    with pytest.raises(ValueError, match="RUN_PROTOCOL_VERSION_MISMATCH"):
        asyncio.run(run_smoke(store=store))
