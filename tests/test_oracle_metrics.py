import json

from delaybind_core.data import canonicalize_record
from delaybind_core.metrics import answer_exact, answer_token_f1, summarize_results
from delaybind_core.operators import execute_operator
from delaybind_core.oracle import compile_oracle_plan, load_oracle_plans, write_oracle_plans


def test_oracle_compiler_hides_compositional_answer_value(tmp_path):
    sample = canonicalize_record(
        {
            "id": "q-oracle",
            "type": "compositional",
            "question": "When did Lothair II's mother die?",
            "answer": "20 March 851",
            "evidences": [
                ["Lothair II", "mother", "Ermengarde of Tours"],
                ["Ermengarde of Tours", "date of death", "20 March 851"],
            ],
            "context": [],
        }
    )
    plan = compile_oracle_plan(sample)
    serialized = plan.model_dump_json()
    assert "20 March 851" not in serialized
    assert plan.answer_contract.target == "?date_of_death"
    output = tmp_path / "plans.json"
    write_oracle_plans({sample.sample_id: plan}, output)
    assert load_oracle_plans(output)[sample.sample_id] == plan


def test_oracle_boolean_comparison_keeps_hidden_values_independent():
    sample = canonicalize_record(
        {
            "id": "q-bool",
            "type": "comparison",
            "question": "Are A and B both located in the same country?",
            "answer": "yes",
            "evidences": [["A", "country", "France"], ["B", "country", "France"]],
            "context": [],
        }
    )
    plan = compile_oracle_plan(sample)
    operator = plan.operators[0]
    assert operator.type == "COMPARE"
    assert operator.inputs[0] != operator.inputs[1]
    assert "France" not in plan.model_dump_json()


def test_oracle_compiler_unifies_evidence_aliases_without_answer_leakage():
    sample = canonicalize_record(
        {
            "id": "q-alias",
            "type": "compositional",
            "question": "What nationality is the composer of song Make The World Move?",
            "answer": "British",
            "evidences": [
                ["Make the World Move", "composer", "Alexander Grant"],
                ["Alex da Kid", "country of citizenship", "British"],
            ],
            "context": [],
        }
    )
    plan = compile_oracle_plan(sample)
    assert plan.patterns[0].object == plan.patterns[1].subject
    assert "British" not in plan.model_dump_json()


def test_answer_and_comparison_metrics_are_deterministic():
    assert answer_exact(True, ["yes"])
    assert answer_token_f1("The Film", ["film"]) == 2 / 3
    assert execute_operator(
        "ARGMIN",
        ["24 December 1886", "15 November 1911"],
        ["Film A", "Film B"],
    ) == "Film A"
    summary = summarize_results(
        [
            {"method": "v5", "order": "original", "answer_exact": True},
            {"method": "v5", "order": "reverse", "answer_exact": False},
        ]
    )
    assert summary["reverse_forward_gap"]["v5"] == 1.0
