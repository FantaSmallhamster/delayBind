"""BIND-only prompt dispatch and evaluation authority, without real API calls."""
import copy
import asyncio
from dataclasses import asdict

import pytest

from delaybind_core.context_r2 import build_update_context
from delaybind_core.prompts_r2 import messages, prompt_version_for, PROMPT_VERSION, MEMORY_BIND_PROMPT_VERSION, SYSTEM_FACT_ONLY
from delaybind_core.protocol_r2 import parse_update, fact_alias_map
from delaybind_core.runner import RunnerConfig
from scripts.evaluate_r2_memory_ab import (assert_frozen_contracts, original_messages, make_payload,
                                         make_case, score, perturb, normalize, metrics, latest_results)
from test_r2_core import fixture, context
from delaybind_core.smoke_r2 import run_smoke


def sample_case():
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False)
    runtime = fixture(config=config)
    runtime.state.run_metadata = {"config": asdict(config)}
    payload = build_update_context(runtime, "Where was Cindy's teacher's mother born?", ["D0:S0"])
    update, errors = parse_update("Q1 | Cindy's father is Elias.\nQ1 | Cindy's teacher is Alice.", payload)
    assert not errors
    runtime.ingest(update, payload)
    ctx = context(runtime, "Q1")
    fid = next(fid for fid in ctx.allowed_fact_ids if runtime.state.facts[fid].text == "Cindy's teacher is Alice.")
    return make_case(case_id="toy.Q1.1", state=runtime.state, ctx=ctx, question=payload["question"],
                     expected=dict(decision="BOUND", value="Alice", support_fact_ids=[fid]), run_id="r")


def test_only_fact_only_bind_and_its_repair_switch_prompts():
    # Historical prompt snapshots remain frozen; production runtime now has a
    # separately versioned member graph. The old live experiment still refuses
    # to run against changed runtime files (default check_runtime=True).
    assert_frozen_contracts(check_runtime=False)
    case = sample_case()
    payload = case["payload"]
    old = original_messages(payload)
    new = messages("MEMORY", payload)
    assert new[0] != old[0]
    assert new[1]["content"].split("Input data:\n", 1)[1] == old[1]["content"].split("Input data:\n", 1)[1]
    assert prompt_version_for("MEMORY", payload) == MEMORY_BIND_PROMPT_VERSION
    rebound = {**payload, "allowed_mode": "REBIND"}
    assert messages("MEMORY", rebound) == original_messages(rebound)
    assert prompt_version_for("MEMORY", rebound) == PROMPT_VERSION
    repair = dict(original_memory_request=payload, validation_errors="TEST_ERROR", rejected_response="NONE")
    assert messages("MEMORY_REPAIR", repair)[0] == new[0]
    assert prompt_version_for("MEMORY_REPAIR", repair) == MEMORY_BIND_PROMPT_VERSION
    repair["original_memory_request"] = rebound
    assert messages("MEMORY_REPAIR", repair)[0]["content"] == SYSTEM_FACT_ONLY
    assert prompt_version_for("MEMORY_REPAIR", repair) == PROMPT_VERSION
    assert prompt_version_for("UPDATE", payload) == PROMPT_VERSION
    assert prompt_version_for("PLAN", payload) == PROMPT_VERSION
    from delaybind_core.prompts_r2 import ANSWER_PROMPT_VERSION
    assert prompt_version_for("ANSWER", payload) == ANSWER_PROMPT_VERSION
    assert prompt_version_for("MEMORY", {**payload, "fact_only": False}) == PROMPT_VERSION


def test_expected_fact_binds_in_isolated_runtime_without_changing_snapshot():
    case = sample_case()
    original = copy.deepcopy(case)
    alias = next(a for a, fid in case["short_to_durable"].items() if fid in case["expected"]["support_fact_ids"])
    good = score(case, f"BOUND | Alice | {alias}")
    assert good["passed"] and good["runtime_valid"]
    assert not good["false_binding"]
    assert case == original
    wrong = score(case, "BOUND | Alice | " + ",".join(case["short_to_durable"]))
    assert wrong["runtime_valid"] and wrong["false_binding"] and not wrong["support_correct"]
    assert not score(case, "NOOP")["passed"]
    missing = score(case, "BOUND | Alice | F999")
    assert not missing["protocol_valid"] and not missing["passed"]


def test_perturbations_remap_support_and_preserve_upstream_and_cardinality():
    base = sample_case()
    texts = [f["text"] for f in base["eligible_facts"]]
    variant = perturb(base, "wrong_entity", ["Mira's teacher is Bob.", *texts], ["Cindy's teacher is Alice."])
    assert variant["cardinality"] == base["cardinality"]
    assert variant["effective_upstream_bindings"] == base["effective_upstream_bindings"]
    assert variant["messages"]["A"][1]["content"].split("Input data:\n", 1)[1] == variant["messages"]["B"][1]["content"].split("Input data:\n", 1)[1]
    assert variant["eligible_facts"][0]["text"] == "Mira's teacher is Bob."
    alias = next(a for a, fid in variant["short_to_durable"].items() if fid in variant["expected"]["support_fact_ids"])
    assert score(variant, f"BOUND | Alice | {alias}")["passed"]
    conflict = perturb(base, "counterevidence", [*texts, "Cindy's teacher is not Alice."], [], decision="NOOP")
    assert score(conflict, "NOOP")["passed"]
    assert normalize(["Alice", "Bob"]) == normalize(["Bob", "Alice"])


def test_api_and_protocol_failures_do_not_count_as_correct_abstentions():
    case = sample_case()
    case["expected"] = dict(decision="NOOP", value=None, support_fact_ids=[])
    a = dict(expected=case["expected"], raw_output=None, api_error="timeout")
    b = dict(expected=case["expected"], raw_output="garbage", score=score(case, "garbage"))
    summary = metrics([a, b])
    assert summary["n"] == 2 and summary["passed"] == 0 and summary["correct_noops"] == 0
    assert summary["api_errors"] == 1 and summary["protocol_errors"] == 1


def test_transport_retry_never_selects_among_multiple_model_answers():
    failed = dict(case_id="toy", version="A", raw_output=None, api_error="HTTP 503")
    answered = dict(case_id="toy", version="A", raw_output="NOOP", api_error=None)
    assert latest_results([failed, answered]) == [answered]
    with pytest.raises(ValueError, match="SEMANTIC_RESAMPLING_FORBIDDEN"):
        latest_results([answered, {**answered, "raw_output": "BOUND | Alice | F1"}])


def test_runner_records_member_version_for_new_architecture():
    from delaybind_core.prompts_r2 import (STRICT_PLAN_PROMPT_VERSION, ANSWER_PROMPT_VERSION, MEMBER_MEMORY_PROMPT_VERSION,
                                           MEMBER_UPDATE_PROMPT_VERSION, MEMBER_RECALL_PROMPT_VERSION)
    from delaybind_core.member_memory_view_r2 import MEMBER_MEMORY_VIEW_VERSION
    result = asyncio.run(run_smoke(outcome="replace", config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False, chunk_size=3, max_model_calls=200)))
    assert result["status"] == "ANSWERED"
    assert result["config"]["memory_bind_prompt_version"] == MEMBER_MEMORY_PROMPT_VERSION
    assert result["config"]["update_prompt_version"] == MEMBER_UPDATE_PROMPT_VERSION
    assert result["config"]["recall_prompt_version"] == MEMBER_RECALL_PROMPT_VERSION
    assert result["config"]["memory_view_version"] == MEMBER_MEMORY_VIEW_VERSION
    requests = [m for m in result["context_manifests"] if m.get("kind") == "MODEL_REQUEST"]
    for m in requests:
        expected = (MEMBER_UPDATE_PROMPT_VERSION if m["interface"] in {"UPDATE", "UPDATE_REPAIR"}
                    else MEMBER_MEMORY_PROMPT_VERSION if m["interface"] in {"MEMORY", "MEMORY_REPAIR"}
                    else MEMBER_RECALL_PROMPT_VERSION if m["interface"] == "RECALL"
                    else STRICT_PLAN_PROMPT_VERSION if m["interface"] == "PLAN"
                    else ANSWER_PROMPT_VERSION)
        assert m["prompt_version"] == expected
    assert next(m for m in requests if m["interface"] == "ANSWER")["memory_view_version"] == MEMBER_MEMORY_VIEW_VERSION
