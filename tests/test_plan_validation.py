import pytest

from delaybind_core.plan_validation import PlanValidationError, ensure_valid_plan, validate_plan
from delaybind_core.schema import QueryPlan


def test_plan_validation_rejects_invented_placeholder_and_unknown_target():
    plan = QueryPlan(
        plan_id="bad",
        patterns=[
            {"id": "p1", "subject": "unknown_person_1", "relation": "director", "object": "Film A"}
        ],
        answer_contract={"target": "?answer", "type": "ENTITY"},
    )
    issues = validate_plan(plan, question="Who directed Film A?")
    assert {issue.code for issue in issues} >= {"INVENTED_PLACEHOLDER", "ANSWER_TARGET_UNKNOWN_VARIABLE"}
    with pytest.raises(PlanValidationError):
        ensure_valid_plan(plan, question="Who directed Film A?")


def test_plan_validation_accepts_question_anchor_and_variables():
    plan = QueryPlan(
        plan_id="good",
        patterns=[
            {"id": "p1", "subject": "Film A", "relation": "director", "object": "?director"}
        ],
        answer_contract={"target": "?director", "type": "ENTITY"},
    )
    assert validate_plan(plan, question="Who directed Film A?") == []


def test_plan_validation_rejects_unanchored_component():
    plan = QueryPlan(
        plan_id="disconnected",
        patterns=[
            {"id": "p1", "subject": "Film A", "relation": "director", "object": "?director"},
            {"id": "p2", "subject": "?mother", "relation": "mother_of", "object": "?person"},
        ],
        answer_contract={"target": "?person", "type": "ENTITY"},
    )
    issues = validate_plan(plan, question="Who directed Film A?")
    assert any(issue.code == "UNANCHORED_PATTERN_COMPONENT" for issue in issues)


def test_plan_validation_rejects_non_reflexive_self_loop():
    plan = QueryPlan(
        plan_id="self-loop",
        patterns=[{"id": "p1", "subject": "?person", "relation": "mother", "object": "?person"}],
        answer_contract={"target": "?person", "type": "ENTITY"},
    )
    issues = validate_plan(plan, question="Who is the mother of Film A?")
    assert any(issue.code == "SELF_LOOP_PATTERN" for issue in issues)


def test_plan_validation_requires_answer_contract_for_runner_plans():
    plan = QueryPlan(
        plan_id="no-answer-contract",
        patterns=[{"id": "p1", "subject": "Film A", "relation": "director", "object": "?director"}],
    )
    issues = validate_plan(plan, question="Who directed Film A?")
    assert any(issue.code == "MISSING_ANSWER_CONTRACT" for issue in issues)


def test_plan_validation_checks_nested_operator_variables():
    plan = QueryPlan(
        plan_id="nested-input",
        patterns=[{"id": "p1", "subject": "Film A", "relation": "director", "object": "?director"}],
        operators=[
            {"id": "count", "type": "COUNT", "inputs": [["?director", "?missing"]], "params": {"output": "?count"}}
        ],
        answer_contract={"target": "?count", "type": "NUMBER"},
    )
    issues = validate_plan(plan, question="Who directed Film A?")
    assert any(issue.code == "OPERATOR_UNKNOWN_VARIABLE" for issue in issues)
