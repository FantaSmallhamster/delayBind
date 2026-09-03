from delaybind_core.prompts import answer_prompt, plan_prompt, update_prompt, verify_prompt
from delaybind_core.schema import QueryPlan


def test_prompt_builders_keep_protocol_markers_and_no_future_instruction():
    plan = QueryPlan(
        plan_id="p",
        patterns=[{"id": "T1", "subject": "Film A", "relation": "DIRECTOR", "object": "?d"}],
    )
    assert "<PLAN version=v1>" in plan_prompt("q", schema={"type": "object"})
    assert "Never invent values" in plan_prompt("q", schema={"type": "object"})
    rendered = update_prompt(
        "q", plan, {}, [{"source_ref": "s1", "text": "fact"}], schema={"type": "object"}
    )
    assert "<UPDATE version=v1>" in rendered
    assert "source_ref" in rendered
    assert "empty events" in rendered
    assert "<VERIFY version=v1>" in verify_prompt(
        "q", {"claim_id": "c"}, [], schema={"type": "object"}
    )
    assert "<ANSWER version=v1>" in answer_prompt("q", {}, schema={"type": "object"})
