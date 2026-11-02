"""Step-1 VERIFY prompt keeps fact fidelity separate from query entailment."""

from delaybind_core.agent_prompts import grounded_memory_json_prompt


def test_grounded_verify_requires_subject_relation_value_and_blocks_proxies():
    prompt = grounded_memory_json_prompt(
        "Where was Ada born?",
        [{
            "id": "Q1", "status": "ACTIVE", "review_pending": False,
            "conflicts": [], "rendered_query": "Where was Ada born?",
            "output": "?birthplace", "depends_on": [],
        }],
        "F1: Ada studied at Sandhurst.",
        hints=[{"text": "Ada may be associated with Essex."}],
        eof=True,
        evidence_contract={"Q1": {"action": "BIND", "allowed_fact_ids": ["F1"]}},
        facts_to_check=[{
            "query_id": "Q1", "fact_id": "F1",
            "fact_text": "Ada studied at Sandhurst.",
            "allowed_context_refs": ["s1"],
        }],
        raw_context="[TARGET] s1 | Ada studied at Sandhurst.",
    )

    assert "SUBJECT --REQUESTED RELATION--> VALUE" in prompt
    assert "education, work" in prompt
    assert "not birthplace or" in prompt
    assert "film release year" in prompt
    assert "Saved plan hints are navigation clues only" in prompt
    assert "leave bindings empty" in prompt
