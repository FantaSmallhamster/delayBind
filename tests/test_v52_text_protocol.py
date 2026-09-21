import asyncio

import pytest

from delaybind_core.agent_prompts_v52 import messages
from delaybind_core.fact_protocol_v52 import parse_update
from delaybind_core.text_protocol_v52 import parse_plan, parse_memory, parse_selection
from delaybind_core.v52_smoke import ScriptedSmokeClient, run_smoke, read_fixture_request


def test_plan_uses_legacy_blocks_without_model_version_tag():
    plan = parse_plan("Q1\nquery: Who teaches Cindy?\noutput: ?teacher\ndepends_on: NONE\n\n"
                      "Q2\nquery: Where was ?teacher born?\noutput: ?city\ndepends_on: Q1")
    assert plan.schema_version == "v3"
    assert plan.queries[1].inputs == {"?teacher": "Q1"}
    with pytest.raises(ValueError, match="DEPENDENCY_TEMPLATE"):
        parse_plan("Q1\nquery: Who?\noutput: ?who\ndepends_on: Q2")
    with pytest.raises(ValueError, match="not a JSON"):
        parse_plan(plan.model_dump_json())


def test_explicit_set_cardinality_without_complete_enumeration():
    plan = parse_plan("Q1\nquery: Which cities?\noutput: ?cities\ndepends_on: NONE\ncardinality: SET")
    assert plan.queries[0].cardinality == "SET"
    assert not plan.queries[0].requires_complete_set


def test_update_legacy_lines_partial_repair_and_escaped_fact():
    raw = r"Q1 | D0:S0 | A \| B\nC | ACTIVE" + "\nQ1 | D0:S9 | future | DORMANT"
    update, rejected = parse_update(raw, query_ids=["Q1"], visible_sources=["D0:S0"])
    assert update.facts[0].text == "A | B\nC"
    assert len(rejected) == 1
    assert parse_update("NONE", query_ids=[], visible_sources=[])[0].facts == []


def test_memory_lines_keep_context_authority_internal_and_validate_entire_proposal():
    proposal = parse_memory("ASSESS | Q1 | F1 | ACCEPT | D0:S0 | RAW_SUPPORTED\n"
                            "BIND | Q1 | Alice | F1\nKEEP | F1", "actual-context")
    assert proposal.context_id == "actual-context"
    assert proposal.operations[1].value == "Alice"
    assert proposal.operations[2].op == "FOCUS"
    with pytest.raises(ValueError):
        parse_memory("BIND | Q1 | Alice | F1\nGARBAGE", "actual-context")
    with pytest.raises(ValueError):
        parse_memory("CONTEXT | forged\nNONE", "actual-context")
    assert parse_selection("SELECT | F1,F2").selected_fact_ids == ["F1", "F2"]


def test_text_input_preserves_multiline_original_and_no_json_output_constraint():
    payload = {"question": "q", "visible_sources": ["D44:S0"], "window_sources": [{"source_ref": "D44:S0", "text": "First line.\n  Second line.\n"}]}
    msg = messages("UPDATE", payload)
    assert "输入 JSON" not in msg[-1]["content"]
    assert "First line.\n" in msg[-1]["content"]
    assert read_fixture_request(msg)["window_sources"][0]["text"] == payload["window_sources"][0]["text"]
    assert "Question:\nq\n\nPlan:\n" in msg[-1]["content"]
    assert "Working memory:\nFact navigation:" in msg[-1]["content"]
    assert "window_sources:" not in msg[-1]["content"]
    class Client(ScriptedSmokeClient):
        async def complete(self, **kwargs):
            assert kwargs.get("response_schema") is None
            return await super().complete(**kwargs)
    result = asyncio.run(run_smoke(client=Client(corrupt_extraction=True)))
    assert result["status"] == "ANSWERED"
    assert result["raw_answer"] == r"\boxed{Suzhou}"
    assert result["interface_calls"].get("VERIFY", 0) == 0


def test_correction_and_hold_commands_keep_scoped_assessment():
    corrected = parse_memory("CORRECT | Q1 | F1 | N1 | Correct text | D0:S0 | D0:S0 | FIX", "c")
    assert corrected.operations[0].correction.local_id == "N1"
    held = parse_memory("ASSESS | Q1 | F1 | HOLD | D0:S0 | NEED_CONTEXT | CONTEXT | D0:S0 | 1 | 0", "c")
    assert held.operations[0].context_request.after == 0
