import json

from delaybind_core.data import canonicalize_record
from delaybind_core.metrics import answer_exact, answer_token_f1, summarize_results, verifier_metrics
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


def test_oracle_compiler_preserves_explicit_question_aliases():
    sample = canonicalize_record(
        {
            "id": "q-anchor",
            "type": "compositional",
            "question": "Where did Maurice, Prince Of Orange's father die?",
            "answer": "Delft",
            "evidences": [
                ["Maurice of Nassau", "father", "William the Silent"],
                ["William the Silent", "place of death", "Delft"],
            ],
            "context": [],
        }
    )
    plan = compile_oracle_plan(sample)
    assert plan.patterns[0].qualifiers["subject_aliases"]
    assert plan.patterns[0].qualifiers["subject_question_anchor"] in plan.patterns[0].qualifiers["subject_aliases"]


def test_oracle_compiler_prefers_supporting_document_title_for_root_alias():
    sample = canonicalize_record(
        {
            "id": "q-support-title",
            "type": "compositional",
            "question": "Where did Maurice, Prince Of Orange's father die?",
            "answer": "Delft",
            "evidences": [
                ["Maurice of Nassau", "father", "William the Silent"],
                ["William the Silent", "place of death", "Delft"],
            ],
            "supporting_facts": [
                ["Maurice, Prince of Orange", 0],
                ["William the Silent", 0],
            ],
            "context": [
                ["Maurice, Prince of Orange", ["He succeeded his father William the Silent."]],
                ["William the Silent", ["He died in Delft."]],
            ],
        }
    )
    plan = compile_oracle_plan(sample)
    first = plan.patterns[0]
    assert first.subject == "Maurice, Prince of Orange"
    assert "Maurice of Nassau" in first.qualifiers["subject_aliases"]
    assert first.qualifiers["binding_policy"] == "PATH_CONSISTENT"
    assert first.qualifiers["candidate_output"] == "?father"


def test_oracle_relation_types_compile_into_query_nodes():
    sample = canonicalize_record(
        {
            "id": "q-types",
            "type": "compositional",
            "question": "What nationality is the director of Blood Street?",
            "answer": "Chinese",
            "evidences": [
                ["Blood Street", "director", "Leo Fong"],
                ["Leo Fong", "country of citizenship", "Chinese"],
            ],
            "context": [],
        }
    )
    plan = compile_oracle_plan(sample)
    graph = __import__(
        "delaybind_core.query_graph", fromlist=["compile_open_query_graph"]
    ).compile_open_query_graph(plan)
    types = {node.symbol: node.value_type for node in graph.nodes}
    assert types == {
        "Blood Street": "FILM",
        "?director": "PERSON",
        "?country_of_citizenship": "NATIONALITY",
    }
    assert plan.answer_contract.type == "NATIONALITY"


def test_oracle_compiler_preserves_parenthetical_entity_disambiguator():
    sample = canonicalize_record(
        {
            "id": "q-disambiguated",
            "type": "compositional",
            "question": "Which country is Aleksander Koniecpolski (1620–1659)'s father from?",
            "answer": "Polish-Lithuanian Commonwealth",
            "evidences": [
                ["Aleksander Koniecpolski", "father", "Stanisław Koniecpolski"],
                ["Stanisław Koniecpolski", "country of citizenship", "Polish-Lithuanian Commonwealth"],
            ],
            "context": [],
        }
    )
    plan = compile_oracle_plan(sample)
    assert plan.patterns[0].qualifiers["subject_question_anchor"] == (
        "Aleksander Koniecpolski (1620–1659)"
    )


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
    assert answer_exact("true", ["yes"])
    assert answer_exact("false", ["no"])
    assert answer_token_f1("The Film", ["film"]) == 2 / 3
    assert execute_operator(
        "ARGMIN",
        ["24 December 1886", "15 November 1911"],
        ["Film A", "Film B"],
    ) == "Film A"
    summary = summarize_results(
        [
            {
                "method": "v5", "order": "original", "answer_exact": True,
                "verifier_tp": 1, "verifier_fp": 1, "verifier_fn": 2,
            },
            {
                "method": "v5", "order": "reverse", "answer_exact": False,
                "verifier_tp": 2, "verifier_fp": 0, "verifier_fn": 1,
            },
        ]
    )
    assert summary["reverse_forward_gap"]["v5"] == 1.0
    original = next(item for item in summary["groups"] if item["order"] == "original")
    assert original["verifier_micro_precision"] == 0.5
    assert original["verifier_micro_recall"] == 1 / 3


def test_verifier_metrics_use_only_terminal_decisions():
    claim = {"claim_id": "c1", "subject": "Film A", "relation": "director", "object": "Martin Lee"}
    wrong = {"claim_id": "c2", "subject": "Film A", "relation": "director", "object": "Other"}
    scored = verifier_metrics(
        [
            {"event_type": "VERIFY_NEED_MORE_CONTEXT", "payload": {"claim": claim}},
            {"event_type": "CLAIM_PROMOTED", "payload": {"claim": claim}},
            {"event_type": "CLAIM_PROMOTED", "payload": {"claim": wrong}},
        ],
        [["Film A", "director", "Martin Lee"]],
    )
    assert scored["verifier_candidate_count"] == 2
    assert scored["verifier_tp"] == 1
    assert scored["verifier_fp"] == 1
    assert scored["verifier_precision"] == 0.5
    assert scored["verifier_recall"] == 1.0
