from delaybind_core import canonicalize_record
from delaybind_core.profiler import infer_answer_contract, profile_sample


def test_profiler_maps_2wiki_question_type_to_contract():
    sample = canonicalize_record(
        {
            "id": "q1",
            "question": "Which film came out first, A or B?",
            "metadata": {
                "type": "comparison",
                "context": {"title": [], "content": []},
            },
        }
    )
    contract = profile_sample(sample)
    assert contract.required_ops == ["COMPARE", "EARLIER"]
    assert contract.answer_contract.type == "ENTITY"


def test_profiler_maps_compositional_question_to_path_join():
    sample = canonicalize_record(
        {
            "id": "q2",
            "question": "Who is the mother of the director of Film A?",
            "metadata": {
                "type": "compositional",
                "context": {"title": [], "content": []},
            },
        }
    )
    assert profile_sample(sample).required_ops == ["PATH_JOIN"]


def test_answer_contract_uses_question_head_not_entity_title_words():
    nationality = canonicalize_record(
        {
            "id": "q-nationality",
            "question": "What nationality is the performer of song When The Stars Go Blue?",
        }
    )
    location = canonicalize_record(
        {
            "id": "q-location",
            "question": "What is the place of birth of the performer?",
        }
    )
    assert infer_answer_contract(nationality).type == "NATIONALITY"
    assert infer_answer_contract(location).type == "LOCATION"
