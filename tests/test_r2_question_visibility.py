"""Only initial planning and final answering see the overall question in R2."""

import copy

import pytest

from delaybind_core.prompts_r2 import (
    MEMBER_UPDATE_CHUNK_PROMPT_VERSION,
    messages,
    prompt_version_for,
)


GLOBAL_QUESTION = "GLOBAL_QUESTION_SENTINEL: Compare the two family trees."
CURRENT_QUERY = "Who teaches Cindy?"
FACT = "Cindy's teacher is Alice."


def payload(*, fact_only=True, member_bindings=True):
    query = dict(id="Q1", template=CURRENT_QUERY, output="?teacher", inputs={},
                 rendered_query=CURRENT_QUERY, bound_inputs={}, member_bindings=member_bindings)
    fact = dict(fact_id="fact-a", text=FACT, source_refs=["D1:S1"], query_id="Q1",
                use_status="CANDIDATE", input_signature="root")
    return dict(
        question=GLOBAL_QUESTION, fact_only=fact_only, member_bindings=member_bindings,
        admission_policy="strict-recall-v1", mode="REPAIR",
        query_graph={"queries": [query]}, query_instance=query,
        working_memory={"navigation": {"facts": [fact]}},
        window_sources=[dict(source_ref="D1:S1", text=FACT)],
        candidate_batch=[fact], selectable_fact_ids=["fact-a"],
        allowed_fact_ids=["fact-a"], allowed_mode="BIND", old_binding=None,
        barriers=[], scope_closed=False, phase="FINAL", required_reviews=[],
        allowed_review_ids=[], staged_reviews=[], context_limits={},
        hint_ids=["fact-a"], repair_targets=[], rejected_items=[], retained_items=[],
        answer_contract="boxed", validation_errors="Test format feedback",
    )


@pytest.mark.parametrize("fact_only,member_bindings", [
    (True, True), (False, True), (True, False), (False, False),
])
@pytest.mark.parametrize("interface,mode", [
    ("UPDATE", "BIND"), ("UPDATE_REPAIR", "BIND"), ("RECALL", "BIND"),
    ("MEMORY", "BIND"), ("MEMORY", "REBIND"),
    ("MEMORY_REPAIR", "BIND"), ("MEMORY_REPAIR", "REBIND"), ("PLAN", "BIND"),
])
def test_intermediate_requests_do_not_read_global_question(interface, mode, fact_only, member_bindings):
    data = payload(fact_only=fact_only, member_bindings=member_bindings)
    data["allowed_mode"] = mode
    if mode == "REBIND":
        data["old_binding"] = dict(value="Alice", members=[
            dict(value="Alice", direct_fact_ids=["fact-a"])])
    request = data
    if interface == "MEMORY_REPAIR":
        request = dict(original_memory_request=data, rejected_response="Alice | F1",
                       validation_errors="Test format feedback")
    rendered = messages(interface, request)
    text = "\n".join(message["content"] for message in rendered)
    assert GLOBAL_QUESTION not in text
    assert "Question:" not in text and "<problem>" not in text
    assert CURRENT_QUERY in text and FACT in text
    if interface in {"UPDATE_REPAIR", "MEMORY_REPAIR", "PLAN"}:
        assert "Test format feedback" in text
    # The model-facing call must not depend on the internal audit field at all.
    data.pop("question")
    assert messages(interface, request) == rendered


@pytest.mark.parametrize("interface", ["UPDATE", "UPDATE_REPAIR"])
@pytest.mark.parametrize("hints", [False, True])
def test_plain_chunk_input_preserves_text_without_problem_wrapper(interface, hints):
    data = payload()
    data.update(chunk_text="  Document 1:\n" + FACT + "\n", plan_hints_enabled=hints)
    rendered = messages(interface, data)
    text = rendered[-1]["content"]
    assert GLOBAL_QUESTION not in text and "<problem>" not in text
    assert "<extraction_targets>\nQ1 | " + CURRENT_QUERY + "\n</extraction_targets>" in text
    assert "<section>\n" + data["chunk_text"] + "\n</section>" in text
    assert prompt_version_for(interface, data) == MEMBER_UPDATE_CHUNK_PROMPT_VERSION
    data.pop("question")
    assert messages(interface, data) == rendered


@pytest.mark.parametrize("interface", ["PLAN", "ANSWER"])
@pytest.mark.parametrize("fact_only,member_bindings", [
    (True, True), (False, True), (True, False), (False, False),
])
def test_initial_plan_and_answer_retain_global_question(interface, fact_only, member_bindings):
    data = payload(fact_only=fact_only, member_bindings=member_bindings)
    data["mode"] = "INITIAL"
    before = copy.deepcopy(data)
    text = "\n".join(message["content"] for message in messages(interface, data))
    assert GLOBAL_QUESTION in text
    assert data == before
