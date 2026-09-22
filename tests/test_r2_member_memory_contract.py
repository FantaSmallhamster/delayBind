"""The isolated evaluator must score binding state, not merely valid syntax."""
import asyncio
import json

from delaybind_core.runner import RunnerConfig
from delaybind_core.storage import SQLiteEventStore
from scripts import evaluate_r2_member_memory_contract as suite


def test_offline_suite_never_constructs_an_api_client(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline verification must not construct an API client")

    monkeypatch.setattr(suite, "OpenAICompatibleClient", forbidden)
    output = tmp_path / "offline"
    summary = asyncio.run(suite.run(output))
    assert summary["mode"] == "offline_fixture"
    assert summary["passed"] == summary["total"] == 8
    retained = json.loads((output / "rebind_keep.json").read_text())
    assert retained["raw_output"] == "NOOP"
    assert retained["context_mode"] == "REBIND"
    assert retained["score"]["final_members"][0]["value"] == "Alice"


def test_valid_noop_cannot_pass_a_supported_first_binding_case():
    store = SQLiteEventStore()
    try:
        case = next(c for c in suite.cases() if c.case_id == "direct")
        config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False)
        runtime, context, _ = suite.prepare(case, store, config)
        result = suite.score(runtime, context, case, "NOOP")
        assert result["protocol_error"] is None
        assert not result["passed"]
        assert not result["state_matches"]
    finally:
        store.close()


def test_valid_protocol_cannot_mask_binding_an_unsupported_entity():
    store = SQLiteEventStore()
    try:
        case = next(c for c in suite.cases() if c.case_id == "wrong_entity")
        config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False)
        runtime, context, _ = suite.prepare(case, store, config)
        raw = suite.canonical_response({"Rowan": case.facts}, context)
        result = suite.score(runtime, context, case, raw)
        assert result["protocol_error"] is None
        assert not result["passed"]
        assert result["final_members"][0]["value"] == "Rowan"
    finally:
        store.close()
