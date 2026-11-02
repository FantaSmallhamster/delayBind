"""V06b MEMORY JSON contracts and adapter semantics."""

import json

import pytest

from delaybind_core.agent_prompts import grounded_memory_json_prompt, memory_json_prompt
from delaybind_core.memory_json import (
    GROUNDED_MEMORY_JSON_SCHEMA,
    MEMORY_JSON_SCHEMA,
    MemoryJSONError,
    grounded_memory_response_format,
    memory_response_format,
    parse_grounded_memory_json,
    parse_memory_json,
)
from delaybind_core.runner import RunnerConfig
from delaybind_core.evaluation import ExperimentConfig


def test_regular_memory_json_preserves_value_types_and_actions():
    parsed = parse_memory_json(json.dumps({
        "bindings": [{"query": "Q1", "value": 7, "support": ["F1"]}],
        "rebindings": [{"query": "Q2", "value": ["A", "B"], "support": ["F2", "F3"]}],
    }), query_ids={"Q1", "Q2"})
    assert parsed.bindings[0].value == 7
    assert parsed.rebindings[0].value == ["A", "B"]
    assert not parsed.rejected_lines


def test_regular_memory_json_keeps_valid_item_when_another_is_bad():
    parsed = parse_memory_json(json.dumps({
        "bindings": [
            {"query": "Q1", "value": "Ada", "support": ["F1"]},
            {"query": "Q9", "value": "bad", "support": ["F9"]},
        ],
        "rebindings": [],
    }), query_ids={"Q1"})
    assert [item.query_id for item in parsed.bindings] == ["Q1"]
    assert len(parsed.rejected_lines) == 1


@pytest.mark.parametrize("raw", [
    "NONE", '{"bindings": [], "rebindings": [], "extra": []}',
    '{"bindings": [], "bindings": [], "rebindings": []}',
])
def test_memory_json_rejects_ambiguous_envelopes(raw):
    with pytest.raises(MemoryJSONError):
        parse_memory_json(raw)


def test_grounded_json_checks_fill_missing_pairs_as_unresolved():
    result = parse_grounded_memory_json(json.dumps({
        "checks": [{"query": "Q1", "fact": "F1", "verdict": "SUPPORTED",
                    "sources": ["S1"], "note": "faithful"}],
        "bindings": [{"query": "Q1", "value": "Ada", "support": ["F1"]}],
        "rebindings": [],
    }), allowed_refs={("Q1", "F1"): {"S1"}, ("Q1", "F2"): {"S2"}}, query_ids={"Q1"})
    assert result.memory.bindings[0].value == "Ada"
    assert {(c.query_id, c.fact_id, c.verdict) for c in result.checks.checks} == {
        ("Q1", "F1", "SUPPORTED"), ("Q1", "F2", "UNRESOLVED")
    }


def test_grounded_json_can_map_exact_displayed_fact_text_to_id():
    result = parse_grounded_memory_json(json.dumps({
        "checks": [{"query": "Q1", "fact": "Ada directed Fern.", "verdict": "SUPPORTED",
                    "sources": ["S1"], "note": "ok"}],
        "bindings": [], "rebindings": [],
    }), allowed_refs={("Q1", "F1"): {"S1"}}, query_ids={"Q1"},
       fact_aliases={("Q1", "Ada directed Fern."): "F1"})
    assert result.checks.checks[0].fact_id == "F1"
    assert result.checks.checks[0].verdict == "SUPPORTED"


def test_memory_json_prompts_remove_text_commands_and_transport_isolated():
    regular = memory_json_prompt("q", [], "NONE", hints=[], eof=True)
    grounded = grounded_memory_json_prompt("q", [], "NONE", hints=[], eof=True,
                                          evidence_contract={}, facts_to_check=[], raw_context="")
    assert '"bindings":[]' in regular and 'BIND |' not in regular
    assert '"checks":[]' in grounded and 'CHECKS\nquery_id' not in grounded
    assert memory_response_format("json_schema")["json_schema"]["strict"] is True
    assert grounded_memory_response_format("json_schema")["json_schema"]["strict"] is True
    assert memory_response_format("prompt") is None
    assert set(MEMORY_JSON_SCHEMA["required"]) == {"bindings", "rebindings"}
    assert set(GROUNDED_MEMORY_JSON_SCHEMA["required"]) == {"checks", "bindings", "rebindings"}


def test_runner_config_has_explicit_memory_protocol():
    assert RunnerConfig().memory_protocol == "text"
    with pytest.raises(ValueError):
        RunnerConfig(memory_protocol="fallback")
    with pytest.raises(ValueError):
        RunnerConfig(memory_json_mode="fallback")
    config = ExperimentConfig.from_mapping({"input": "unused", "runner": {
        "memory_protocol": "json", "memory_json_mode": "json_object"}})
    assert config.runner.memory_protocol == "json"
    assert config.runner.memory_json_mode == "json_object"
