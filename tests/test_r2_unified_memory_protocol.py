"""The unified wire boundary returns evidence-backed answers, never write commands."""

import pytest

from delaybind_core.memory_repair_hints_r2 import fact_only_repair_hint
from delaybind_core.prompts_r2 import (
    MEMBER_MEMORY_PROMPT_VERSION,
    UNIFIED_MEMORY_PROMPT,
    UNIFIED_MEMORY_PROMPT_VERSION,
    messages,
    prompt_version_for,
    request_view,
)
from delaybind_core.protocol_r2 import parse_memory, parse_unified_memory
from delaybind_core.schema_r2 import MemoryContextR2, UNIFIED_MEMORY_INTERFACE


def context(**updates):
    data = dict(
        context_id="CTX-unified", review_id="RV-unified", state_revision=3,
        allowed_mode="BIND", phase="FINAL", query_id="Q2", query_version=1,
        input_signature="input-signature", expected_binding_id=None, inbox_revision=1,
        candidate_bucket_version="bucket", required_reviews=[], allowed_review_ids=[],
        allowed_fact_ids=["durable-a", "durable-b", "durable-c"],
        visible_source_refs=[], raw_hashes={}, barriers={}, scope_closed=False,
        context_limits={}, fact_only=True, member_bindings=True,
        memory_interface=UNIFIED_MEMORY_INTERFACE,
        fact_aliases={"F1": "durable-a", "F2": "durable-b", "F3": "durable-c"},
        authorized_use_ids=["U1", "U2", "U3"], evidence_digest="digest",
        evidence_origins=["UPDATE"],
        working_memory={"navigation": {"facts": [
            {"fact_id": "durable-a", "text": "Bob's mother is Mary."},
            {"fact_id": "durable-b", "text": "Mary is also known as M7."},
            {"fact_id": "durable-c", "text": "M7 is Bob's mother."},
        ]}},
    )
    data.update(updates)
    return MemoryContextR2(**data)


def payload(ctx=None, **updates):
    data = (ctx or context()).model_dump(mode="json")
    data.update(question="TOTAL_QUESTION_MUST_STAY_PRIVATE", old_binding={
        "value": "PREVIOUS_ANSWER_MUST_STAY_PRIVATE",
        "members": [{"value": "PREVIOUS_ANSWER_MUST_STAY_PRIVATE", "direct_fact_ids": ["durable-a"]}]},
        query_instance={"id": "Q2", "template": "Who is ?person's mother?",
                        "rendered_query": "Who is Bob's mother?", "output": "?mother",
                        "bound_inputs": {"?person": "Bob"}})
    data.update(updates)
    return data


def repair(original, rejected="Q2 | F99 | Mary", error="UNKNOWN_FACT_ALIAS:F99"):
    return dict(original_memory_request=original, rejected_response=rejected, validation_errors=error)


def result(raw, ctx=None):
    return parse_memory(raw, ctx or context()).operations[0].binding_result


def test_input_and_prompt_identical_across_internal_sources_and_write_modes():
    requests = [payload(allowed_mode="BIND", evidence_origins=["UPDATE"]),
                payload(allowed_mode="REBIND", evidence_origins=["PRIOR_SUPPORT", "UPDATE"]),
                payload(allowed_mode="BIND", evidence_origins=["DEFERRED"])]
    rendered = [messages("MEMORY", data) for data in requests]
    assert rendered[0] == rendered[1] == rendered[2]
    content = "\n".join(message["content"] for message in rendered[0])
    for hidden in ("BIND", "REBIND", "RECALL", "Mode:", "Existing binding:", "Evidence mode:",
                   "TOTAL_QUESTION", "PREVIOUS_ANSWER", "DEFERRED", "durable-", "input-signature", "U1"):
        assert hidden not in content
    view = rendered[0][-1]["content"].split("Input data:\n", 1)[1]
    assert view == ("Current query:\nQ2 | Who is Bob's mother?\n\nFacts:\n"
                    "F1 | Bob's mother is Mary.\nF2 | Mary is also known as M7.\nF3 | M7 is Bob's mother.")
    assert request_view("MEMORY", requests[0]) == view


def test_inputs_visible_only_when_runtime_authorizes_upstream_inference():
    assert "Inputs:" not in request_view("MEMORY", payload())
    assert "Inputs:\n?person=Bob" in request_view("MEMORY", payload(upstream_only_allowed=True))
    with pytest.raises(ValueError, match="UNIFIED_MISSING_FACT_SUPPORT"):
        result("Q2 | NONE | Mary")
    bound = result("Q2 | NONE | Mary", context(upstream_only_allowed=True))
    assert bound.state == "BOUND" and bound.kind == "INFERRED"
    assert bound.members[0].support_fact_ids == []


def test_same_answer_merges_support_without_losing_other_members():
    response = parse_memory("Q2 | F1 | Mary\nQ2 | F2,F3 | Mary\nQ2 | F2 | Helen", context(allowed_mode="REBIND"))
    action = response.operations[0]
    assert action.op == "REBIND" and action.query_id == "Q2"
    assert action.binding_result.reason_code == "SUPPORTED_RESULT"
    assert [(member.value, member.support_fact_ids) for member in action.binding_result.members] == [
        ("Mary", ["durable-a", "durable-b", "durable-c"]), ("Helen", ["durable-b"])]
    assert response.ignored_lines == [] and response.format_normalizations == []


def test_unknown_is_explicit_unsupported_result_without_evidence_or_members():
    response = parse_memory("Q2 | NONE | UNKNOWN", context(allowed_mode="REBIND"))
    binding = response.operations[0].binding_result
    assert response.operations[0].op == "REBIND"
    assert binding.state == "NOOP" and binding.reason_code == "UNSUPPORTED_RESULT"
    assert binding.value is None and binding.members == [] and binding.support_fact_ids == []


@pytest.mark.parametrize(("raw", "error"), [
    ("NONE", "FIELD_COUNT"), ("NOOP", "FIELD_COUNT"),
    ("BOUND | Mary | F1", "QUERY_MISMATCH"),
    ("Q3 | F1 | Mary", "QUERY_MISMATCH"),
    ("Q2 | F1 | Mary\nQ3 | F2 | Helen", "QUERY_MISMATCH"),
    ("Q2 | F1 | Mary\nmalformed", "FIELD_COUNT"),
    ("Q2 | F1 | Mary | explanation", "FIELD_COUNT"),
    ("Q2 | F1 | Mary\nQ2 | NONE | UNKNOWN", "UNKNOWN_REQUIRES_STANDALONE"),
    ("Q2 | F1 | UNKNOWN", "UNKNOWN_REQUIRES_STANDALONE"),
    ("Q2 | NONE | UNKNOWN\nQ2 | NONE | UNKNOWN", "UNKNOWN_REQUIRES_STANDALONE"),
    ("Q2 | F9 | Mary", "UNKNOWN_FACT_ALIAS"),
    ("Q2 | durable-a | Mary", "UNKNOWN_FACT_ALIAS"),
    ("Q2 | F1,F1 | Mary", "DUPLICATE_SUPPORT_FACT"),
    ("Q2 | F1, | Mary", "INVALID_SUPPORT_FIELD"),
    ("Q2 | | Mary", "INVALID_SUPPORT_FIELD"),
    ("Q2 | F1 | [\"Mary\", \"Helen\"]", "ONE_CONCRETE_ANSWER"),
    ("Q2 | F1 | {\"answer\":\"Mary\"}", "ONE_CONCRETE_ANSWER"),
    ("Q2 | F1 | null", "ONE_CONCRETE_ANSWER"),
    ("Q2 | F1 | ?mother", "ONE_CONCRETE_ANSWER"),
    ("Q2 | F1 |", "ONE_CONCRETE_ANSWER"),
    ("Q2 | F1 | NaN", "ONE_CONCRETE_ANSWER"),
])
def test_rejects_entire_damaged_answer_set(raw, error):
    ctx = context()
    before = ctx.model_dump(mode="json")
    with pytest.raises(ValueError, match=error):
        parse_memory(raw, ctx)
    assert ctx.model_dump(mode="json") == before


@pytest.mark.parametrize("answer", ["Trinidad and Tobago", "Smith, Jones and Co.", "Research and Development"])
def test_names_with_commas_or_conjunctions_remain_one_entity(answer):
    binding = result("Q2 | F1 | " + answer)
    assert len(binding.members) == 1 and binding.members[0].value == answer


def test_escaped_values_preserve_literal_pipe_backslash_and_newline():
    binding = result(r"Q2 | F1 | Research \| Development\\Lab\nEast Wing")
    assert binding.members[0].value == "Research | Development\\Lab\nEast Wing"


@pytest.mark.parametrize("mutate", [
    lambda data: data["fact_aliases"].pop("F1"),
    lambda data: data["fact_aliases"].update({"X1": data["fact_aliases"].pop("F1")}),
    lambda data: data["working_memory"]["navigation"]["facts"].pop(),
    lambda data: data["working_memory"]["navigation"]["facts"].append({"fact_id": "hidden", "text": "Hidden fact."}),
    lambda data: data["working_memory"]["navigation"]["facts"][0].update(text=""),
])
def test_rendering_and_parser_share_exact_display_and_authority_checks(mutate):
    data = context().model_dump(mode="json")
    mutate(data)
    with pytest.raises(ValueError, match="WHITELIST_MISMATCH"):
        messages("MEMORY", payload(MemoryContextR2(**data)))
    with pytest.raises(ValueError, match="WHITELIST_MISMATCH"):
        parse_memory("Q2 | F1 | Mary", MemoryContextR2(**data))


def test_frozen_aliases_are_used_instead_of_recomputed_sort_order():
    ctx = context(fact_aliases={"F3": "durable-a", "F1": "durable-b", "F2": "durable-c"})
    assert "F3 | Bob's mother is Mary." in request_view("MEMORY", payload(ctx))
    assert result("Q2 | F3 | Mary", ctx).support_fact_ids == ["durable-a"]


def test_repair_uses_same_task_prompt_and_complete_replacement_protocol():
    data = payload()
    normal = messages("MEMORY", data)
    repair_data = repair(data)
    repaired = messages("MEMORY_REPAIR", repair_data)
    assert normal[0] == repaired[0]
    assert normal[1]["content"].split("Input data:\n")[0] == repaired[1]["content"].split("Input data:\n")[0]
    assert UNIFIED_MEMORY_PROMPT in repaired[1]["content"]
    assert "complete replacement answer set" in repaired[1]["content"]
    assert "Q2 | NONE | UNKNOWN" in repaired[1]["content"]
    assert "UNKNOWN_FACT_ALIAS:F99" in repaired[1]["content"]
    assert "TOTAL_QUESTION" not in repaired[1]["content"]
    hint = fact_only_repair_hint(repair_data)
    assert all(operation not in hint for operation in ("BOUND", "NOOP", "BIND", "REBIND"))
    assert prompt_version_for("MEMORY_REPAIR", repair_data) == UNIFIED_MEMORY_PROMPT_VERSION


def test_protocol_is_selected_explicitly_and_legacy_remains_compatible():
    assert prompt_version_for("MEMORY", payload()) == "query-facts-result-v1"
    legacy = context(memory_interface="legacy_bind_rebind_v1")
    assert result("BOUND | Mary | F1", legacy).state == "BOUND"
    assert result("NONE", legacy).state == "NOOP"
    with pytest.raises(ValueError, match="INVALID_FACT_ONLY_MEMORY_FIELD"):
        result("Q2 | F1 | Mary", legacy)
    assert prompt_version_for("MEMORY", payload(legacy)) == MEMBER_MEMORY_PROMPT_VERSION


@pytest.mark.parametrize("updates", [{"fact_only": False}, {"member_bindings": False}, {"phase": "REVIEW"}])
def test_unified_parser_and_renderer_reject_incompatible_context(updates):
    ctx = context(**updates)
    with pytest.raises(ValueError, match="REQUIRES_FACT_ONLY_MEMBER_FINAL"):
        parse_unified_memory("Q2 | F1 | Mary", ctx)
    with pytest.raises(ValueError, match="REQUIRES_FACT_ONLY_MEMBER_FINAL"):
        messages("MEMORY", payload(ctx))


def test_empty_facts_is_a_valid_request_and_unknown_result():
    ctx = context(allowed_fact_ids=[], fact_aliases={}, working_memory={"navigation": {"facts": []}})
    assert "Facts:\nNONE" in request_view("MEMORY", payload(ctx))
    assert result("Q2 | NONE | UNKNOWN", ctx).reason_code == "UNSUPPORTED_RESULT"
