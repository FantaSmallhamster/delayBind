from delaybind_core.prompts import answer_prompt, plan_prompt, update_prompt, verify_prompt
from delaybind_core.schema import QueryPlan, VerifyDecision


def test_prompt_builders_keep_protocol_markers_and_no_future_instruction():
    plan = QueryPlan(
        plan_id="p",
        patterns=[{"id": "T1", "subject": "Film A", "relation": "DIRECTOR", "object": "?d"}],
    )
    assert "<PLAN version=v2>" in plan_prompt("q", schema={"type": "object"})
    assert "Never invent values" in plan_prompt("q", schema={"type": "object"})
    assert "Do not generate answer_contract" in plan_prompt(
        "q", schema={"type": "object"}, require_answer_target=False
    )
    rendered = update_prompt(
        "q", plan, {}, [{"source_ref": "s1", "text": "fact"}], schema={"type": "object"}
    )
    assert "<UPDATE version=v2>" in rendered
    assert "source_ref" in rendered
    assert "empty events" in rendered
    rendered_verify = verify_prompt(
        "q", {"claim_id": "c"}, [], schema={"type": "object"}
    )
    assert "<VERIFY version=v2>" in rendered_verify
    assert "non-empty reason" in rendered_verify
    assert "B is A's father" in rendered_verify
    assert "queen by marriage" in rendered_verify
    assert "Claim" not in VerifyDecision.model_json_schema().get("$defs", {})
    rendered_answer = answer_prompt("q", {}, schema={"type": "object"})
    assert "<ANSWER version=v2>" in rendered_answer
    assert "source_refs" in rendered_answer
