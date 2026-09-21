import asyncio
import json
from dataclasses import replace
import pytest

from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.smoke_r2 import run_smoke, ScriptedR2Client, read_request
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.agents_v52 import LowLevelAgentV52
from delaybind_core.data import canonicalize_record, build_manifest
from delaybind_core.v52_smoke import smoke_plan
from delaybind_core.schema_v52 import QueryPlanV3


@pytest.mark.parametrize("outcome,answer,created,retired", [("keep", "Suzhou", 3, 0), ("replace", "Beijing", 6, 3), ("unbound", None, 3, 3)])
def test_three_rebind_outcomes_are_end_to_end_text_only(outcome, answer, created, retired):
    client = ScriptedR2Client()
    result = asyncio.run(run_smoke(outcome=outcome, client=client))
    assert result["status"] == ("INSUFFICIENT" if answer is None else "ANSWERED"), result["reason_codes"]
    assert result["answer"]["answer"] == answer
    assert result["r2_metrics"]["bindings_created"] == created
    assert result["r2_metrics"]["bindings_retired"] == retired
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1
    assert result["interface_calls"].get("VERIFY", 0) == 0
    assert result["interface_calls"]["PLAN"] == 1
    assert result["interface_calls"]["RECALL"] >= 2
    for interface, data, audit in client.calls:
        assert audit["protocol_version"] == "v5.2-r2"
        if interface == "ANSWER":
            assert "\n\nPlan:" not in data["body"] and "\n\nCandidates:" not in data["body"]
            assert result["answer"]["source_refs"] == []
        elif interface == "MEMORY":
            assert audit["context_id"] and audit["review_id"]
            assert audit["memory_mode"] in {"BIND", "REBIND"}
            assert audit["review_phase"] in {"REVIEW", "FINAL"}


def test_r07_recall_failure_is_not_empty_selection_and_answer_does_not_run_early():
    class Broken(ScriptedR2Client):
        async def complete(self, **kwargs):
            if kwargs["interface"] == "RECALL":
                return "SELECT F_not_selectable"
            return await super().complete(**kwargs)
    result = asyncio.run(run_smoke(client=Broken()))
    assert result["status"] == "RUNTIME_ERROR"
    assert "SCAN_INCOMPLETE" in result["reason_codes"]
    assert any(j["status"] == "SCAN_INCOMPLETE" for j in result["state"]["jobs"].values())
    assert result["state"]["executions"]["Q2"]["current_binding_id"] is None
    assert result["interface_calls"].get("ANSWER", 0) == 0


def test_api_failure_during_rebind_leaves_old_chain_blocked_not_semantically_unbound():
    class Broken(ScriptedR2Client):
        async def complete(self, **kwargs):
            if kwargs["interface"] == "MEMORY" and kwargs["local_metadata"]["memory_mode"] == "REBIND":
                raise TimeoutError("fixture timeout")
            return await super().complete(**kwargs)
    result = asyncio.run(run_smoke(client=Broken()))
    assert result["status"] == "RUNTIME_ERROR"
    assert result["state"]["executions"]["Q1"]["current_binding_id"] == "B1"
    assert result["state"]["executions"]["Q1"]["blocking_review_ids"]
    assert result["state"]["bindings"] == {}
    assert result["evidence_pack"]["navigation"]["facts"] == []
    assert result["r2_metrics"]["bindings_retired"] == 0
    assert result["interface_calls"].get("ANSWER", 0) == 0


def test_memory_repair_uses_identical_context_without_partial_commit():
    class Repair(ScriptedR2Client):
        failed = None
        async def complete(self, **kwargs):
            if kwargs["interface"] == "MEMORY" and self.failed is None:
                self.failed = kwargs["local_metadata"]
                return "NONE"
            if kwargs["interface"] == "MEMORY_REPAIR":
                assert kwargs["local_metadata"] == self.failed
            return await super().complete(**kwargs)
    result = asyncio.run(run_smoke(client=Repair()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["interface_calls"]["MEMORY_REPAIR"] == 1
    assert len(result["state"]["binding_store"]) == 3


@pytest.mark.parametrize("budget,code", [({"max_review_input_tokens": 1}, "REVIEW_INPUT_BUDGET"),
                                        ({"max_answer_input_tokens": 1}, "FINAL_RAW_MEMORY_BUDGET"),
                                        ({"max_model_calls": 3}, "MODEL_CALL_BUDGET")])
def test_r09_resource_limits_are_explicit_and_do_not_drop_required_raw(budget, code):
    result = asyncio.run(run_smoke(config=RunnerConfig(protocol_version="v5.2-r2", chunk_size=32, **budget)))
    assert result["status"] == "RESOURCE_LIMIT"
    assert code in result["reason_codes"]
    if code == "FINAL_RAW_MEMORY_BUDGET":
        assert result["evidence_pack"]["raw_evidence"] and result["answer"] is None
    else:
        assert result["interface_calls"].get("ANSWER", 0) == 0


def test_r14_empty_memory_still_calls_answer():
    class Empty(ScriptedR2Client):
        async def complete(self, **kwargs):
            if kwargs["interface"] == "UPDATE":
                return "NONE"
            return await super().complete(**kwargs)
    result = asyncio.run(run_smoke(client=Empty()))
    assert result["status"] == "INSUFFICIENT"
    assert result["interface_calls"]["ANSWER"] == 1
    assert result["evidence_pack"]["raw_evidence"] == []


def test_r11_completed_resume_is_call_free():
    store, client = SQLiteEventStore(), ScriptedR2Client()
    first = asyncio.run(run_smoke(store=store, client=client))
    count = len(client.calls)
    second = asyncio.run(run_smoke(store=store, client=client))
    assert second["state"] == first["state"] and len(client.calls) == count


def test_p16_low_verify_is_forbidden():
    with pytest.raises(PermissionError, match="INTERFACE_NOT_AUTHORIZED"):
        asyncio.run(LowLevelAgentV52(ScriptedR2Client()).call("VERIFY"))


def test_r18_disabled_segmentation_is_fact_only_without_model_visible_raw(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("syntok must not run while disabled")
    monkeypatch.setattr("delaybind_core.sentence_index.sentence_spans", forbidden)
    monkeypatch.setattr("delaybind_core.sentence_index.text_manifest", forbidden)
    text = ("Document 18:\nCindy\nCindy's teacher is Alice.\n"
            "Document 44:\nAlice\nAlice's mother is Mary.\n"
            "Document 45:\nMary\nMary was born in Suzhou.")
    client = ScriptedR2Client()
    result = asyncio.run(V5Runner(client, config=RunnerConfig(protocol_version="v5.2-r2",
        sentence_splitting=False, chunk_size=1000)).run(run_id="chunks", question="Where?", context=text,
        store=SQLiteEventStore(), plan=smoke_plan()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Suzhou"
    assert result["evidence_pack"]["raw_evidence"] == []
    assert result["state"]["fact_only"] is True
    assert result["state"]["review_records"] == {}
    assert all(not f["source_refs"] for f in result["evidence_pack"]["navigation"]["facts"])
    for interface, data, _audit in client.calls:
        if interface != "PLAN":
            assert data["fact_only"] is True
            assert data["raw"] == []
            assert not any(ref in data["body"] for ref in ("D18@C0", "D44@C0", "D45@C0"))
    requests = [m for m in result["context_manifests"] if m.get("kind") == "MODEL_REQUEST"]
    assert requests and all(m["visible_source_refs"] == [] for m in requests)
    update_bodies = [data["body"] for interface, data, _audit in client.calls if interface == "UPDATE"]
    assert update_bodies and any("<DOCUMENT_BOUNDARY>" in body for body in update_bodies)


def test_fact_only_late_resolved_fact_rebinds_and_reactivates_descendants():
    client = ScriptedR2Client()
    result = asyncio.run(run_smoke(outcome="replace", client=client, config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False, chunk_size=3, max_model_calls=200)))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Beijing"
    assert result["r2_metrics"]["bindings_created"] == 6
    assert result["r2_metrics"]["bindings_retired"] == 3
    assert result["state"]["bindings"] == {"?teacher": "Bob", "?mother": "June", "?city": "Beijing"}
    retired = [e for e in result["events"] if e["event_type"] == "BINDING_RETIRED"]
    assert {e["payload"]["query_id"] for e in retired} == {"Q1", "Q2", "Q3"}
    rebound = [e for e in result["events"] if e["event_type"] == "QUERY_ACTIVATED"]
    assert any(e["payload"]["query_id"] == "Q2" for e in rebound)
    assert all(not b["source_refs"] for b in result["state"]["binding_store"].values())


def test_fact_only_legacy_unbound_is_noop_and_preserves_old_chain():
    result = asyncio.run(run_smoke(outcome="unbound", client=ScriptedR2Client(), config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False, chunk_size=3, max_model_calls=200)))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["answer"]["answer"] == "Suzhou"
    assert result["state"]["bindings"] == {"?teacher": "Alice", "?mother": "Mary", "?city": "Suzhou"}
    assert result["r2_metrics"]["bindings_created"] == 3
    assert result["r2_metrics"]["bindings_retired"] == 0
    assert any(e["event_type"] == "FACT_ONLY_DECISION_NOOP" for e in result["events"])
    assert not any(e["event_type"] == "BINDING_RETIRED" for e in result["events"])


def test_r16_nocallback_does_not_disable_rebind():
    result = asyncio.run(run_smoke(config=RunnerConfig(protocol_version="v5.2-r2", chunk_size=32, enable_defer_callback=False)))
    assert result["interface_calls"].get("RECALL", 0) == 0
    assert result["r2_metrics"]["rebind_requests"] == 1
    assert result["r2_metrics"]["reaffirmations"] == 1


def test_r16_cli_and_evaluation_dispatch_version_text_plan_and_metrics(tmp_path, monkeypatch):
    from delaybind_core import cli
    from delaybind_core.api import APIConfig
    from delaybind_core.evaluation import ExperimentConfig, run_experiment
    sample = {"id": "s", "question": "Where was Cindy's teacher's mother born?", "answer": "Suzhou", "context": [
        ["Mary", ["Mary was born in Suzhou."]], ["Alice", ["Alice's mother is Mary."]], ["Cindy", ["Cindy's teacher is Alice."]]]}
    data = tmp_path / "data.json"
    data.write_text(json.dumps([sample]))
    conf = tmp_path / "config.json"
    conf.write_text(json.dumps(dict(input=str(data), runner=dict(protocol_version="v5.2-r2", chunk_size=32))))
    monkeypatch.setattr(cli, "_load_env_file", lambda: None)
    monkeypatch.setattr(cli, "_api_config", lambda *a, **kw: APIConfig(base_url="http://unused", model="fixture", api_key="fixture"))
    monkeypatch.setattr(cli, "OpenAICompatibleClient", lambda *a, **kw: ScriptedR2Client())
    result = cli._run_from_config(conf)
    assert result["protocol_version"] == "v5.2-r2" and result["status"] == "ANSWERED"
    assert cli.build_parser().parse_args(["run", "--config", str(conf), "--protocol-version", "v5.2-r2", "--no-sentence-splitting"]).sentence_splitting is False
    ec = ExperimentConfig.from_mapping(dict(input=str(data), output_dir=str(tmp_path / "results"), methods=["v52_r2_predicted"],
        orders=["original"], sample_count=1, runner=dict(chunk_size=32)))
    asyncio.run(run_experiment(ec, client_factory=lambda store: ScriptedR2Client()))
    scored = json.loads((tmp_path / "results" / "results.jsonl").read_text())
    assert scored["protocol_version"] == "v5.2-r2" and scored["answer_exact"] == 1.0
    assert scored["transaction_replay_consistency"] == 1 and scored["run_success"]


def test_r16_adapter_passes_version_and_nocallback(monkeypatch):
    from taskutils.memory_eval.utils import v52_r2
    captured = {}
    async def fake(*args, **kwargs):
        captured.update(kwargs)
        captured["nocallback"] = args[-1]
        return "fixture"
    monkeypatch.setattr(v52_r2, "_query", fake)
    assert asyncio.run(v52_r2.async_query_llm({}, "model", None, nocallback=True)) == "fixture"
    assert captured == {"protocol_version": "v5.2-r2", "nocallback": True}


def test_old_config_fingerprints_exclude_unused_r2_settings():
    for version in ("v5", "v5.1", "v5.2"):
        settings = RunnerConfig(protocol_version=version).protocol_mapping()
        assert "memory_contract" not in settings and "plan_repair_mode" not in settings
        assert settings["protocol_version"] == version
    assert RunnerConfig(protocol_version="v5.2-r2").protocol_mapping()["memory_contract"] == "bind-rebind-1"
