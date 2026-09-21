"""No paid calls: prove display-only controls, mapping authority and isolation."""
import copy
import asyncio
import json
from unittest.mock import patch

import pytest

from delaybind_core import prompts_r2 as p
from delaybind_core.memory_experiments_r2 import experimental_messages, experimental_view
from scripts import evaluate_r2_memory_isolation as ex
from scripts import evaluate_r2_memory_ab as ab
from test_r2_memory_ab import sample_case


def condition(case, version="B", **options):
    return ex.make_condition(case, "test", version, "1", **options)


def test_existing_a_b_and_full_render_are_byte_identical():
    ab.assert_frozen_contracts(check_runtime=False)
    c = sample_case()
    assert experimental_messages(c, "A") == ab.original_messages(c["payload"])
    assert experimental_messages(c, "B") == p.messages("MEMORY", c["payload"])
    assert experimental_messages(c, "B")[0] == experimental_messages(c, "P17-full")[0]
    assert experimental_view(c) == p.request_view("MEMORY", c["payload"])
    restored = p.MEMORY_FACT_ONLY_NEUTRAL
    for old, new in (("Anne de Mowbray", "Elara Venn"), ("Richard of Shrewsbury", "Corvin Dale"),
                     ("Cindy", "Neris Moss"), ("Bob", "Torin Vale"), ("Alice", "Selene Hart")):
        restored = restored.replace(new, old)
    assert restored == p.MEMORY_FACT_ONLY
    assert "示例" not in p.MEMORY_BIND_P17


def test_display_flags_never_change_graph_or_context():
    c = sample_case()
    original = copy.deepcopy(c)
    neutral = experimental_view(c, neutral_output=True)
    assert "output=?result" in neutral and "Question:" in neutral
    hidden = experimental_view(c, hide_question=True)
    assert "Question:" not in hidden and "output=?teacher" in hidden
    local = experimental_view(c, local=True)
    for forbidden in ("Question:", "output=", "Q1 |", "Existing binding:", "Mode:"):
        assert forbidden not in local
    assert "Facts:" in local and "cardinality=SINGLE" in local
    assert c == original


def test_order_and_aliases_are_independent_and_support_is_remapped():
    c = sample_case()
    texts = [f["text"] for f in c["eligible_facts"]]
    reverse = ex.ordered(c, list(reversed(texts)))
    assert {f["short_id"]: f["durable_id"] for f in reverse} == c["short_to_durable"]
    ids = ex.ordered(c, texts, {t: f"F{len(texts)-i}" for i, t in enumerate(texts)})
    assert [f["text"] for f in ids] == texts
    cond = condition(c, facts=ids)
    correct = next(f["short_id"] for f in ids if f["text"] == "Cindy's teacher is Alice.")
    assert ex.production_score(c, cond, f"BOUND | Alice | {correct}")["passed"]
    wrong = next(f["short_id"] for f in ids if f["short_id"] != correct)
    assert not ex.production_score(c, cond, f"BOUND | Alice | {wrong}")["passed"]
    assert not ex.production_score(c, cond, "BOUND | Alice | F999")["protocol_valid"]
    assert not ex.production_score(c, cond, f"BOUND | Alice | {correct},{correct}")["protocol_valid"]


def test_noise_insertion_preserves_old_ids():
    c = sample_case()
    ts = [f["text"] for f in c["eligible_facts"]]
    variant = ex.changed(c, "noise", ["A bird sings.", *ts], ["Cindy's teacher is Alice."])
    actual = {f["text"]: f["short_id"] for f in variant["eligible_facts"]}
    assert all(actual[f["text"]] == f["short_id"] for f in c["eligible_facts"])
    assert actual["A bird sings."] == "F3"


def test_diagnostic_never_enters_production_parser_or_runtime():
    c = sample_case()
    original = copy.deepcopy(c)
    cond = condition(c, "diagnostic")
    aliases = {f["text"]: f["short_id"] for f in c["eligible_facts"]}
    raw = json.dumps(dict(answer_candidates=[dict(value="Alice", support_fact_ids=[aliases["Cindy's teacher is Alice."]])],
                          excluded_facts=[dict(fact_id=aliases["Cindy's father is Elias."], reason_code="RELATION_MISMATCH")],
                          blocking_fact_ids=[], decision="BOUND"))
    with patch.object(ab, "score", side_effect=AssertionError("NO RUNTIME")):
        scored = ex.diagnostic_score(c, cond, raw)
    assert scored["passed"] and not scored["runtime_committed"]
    assert c == original
    assert not ex.diagnostic_score(c, cond, raw.replace('"BOUND"', '"UNBOUND"'))["protocol_valid"]
    assert not ex.diagnostic_score(c, cond, raw.replace('"blocking_fact_ids": []', '"blocking_fact_ids": ["F99"]'))["protocol_valid"]


def test_repeats_are_separate_samples_not_best_of_three():
    row = dict(stage="1", condition_id="c", version="B", repeat=1, raw_output="NOOP")
    assert len(ex.latest([row, {**row, "repeat": 2}, {**row, "repeat": 3}])) == 3
    with pytest.raises(ValueError, match="SEMANTIC_RESAMPLING"):
        ex.latest([row, {**row, "raw_output": "BOUND | Alice | F1"}])
    failed = {**row, "raw_output": None, "api_error": "503"}
    assert list(ex.latest([failed, row]).values()) == [row]


def test_cannot_select_from_a_partial_repeat_batch(tmp_path):
    ex.save_lines(tmp_path / "stage1_conditions.jsonl", [dict(condition_id="c", version="B")])
    row = dict(stage="1", condition_id="c", version="B", repeat=1, raw_output="NOOP")
    ex.save_lines(tmp_path / "results.jsonl", [row])
    with pytest.raises(AssertionError, match="2 unanswered"):
        ex.require_complete(tmp_path, "1")


def test_new_projection_passes_real_runtime_and_does_not_use_model_output():
    c = ex.new_projection(case_id="heldout.test", question="Where was the film's director born?",
                          query="Who directed Film Aspen?", output_variable="?director",
                          texts=["Nola Reed directed Film Aspen."], value="Nola Reed", support=[0])
    assert ex.production_score(c, condition(c), "BOUND | Nola Reed | F1")["passed"]
    assert c["context"]["fact_only"] and c["context"]["allowed_mode"] == "BIND"


def test_stage3_only_changes_bind_and_validates_frozen_window():
    from scripts.evaluate_r2_memory_chain import stage3_messages, FrozenReader
    c = sample_case()
    old = p.messages("MEMORY", c["payload"])
    new = stage3_messages("MEMORY", c["payload"])
    assert new[0] == old[0]
    assert new[1]["content"] == old[1]["content"].replace(p.MEMORY_FACT_ONLY_BIND, p.MEMORY_BIND_P17, 1)
    rebound = {**c["payload"], "allowed_mode": "REBIND"}
    assert stage3_messages("MEMORY", rebound) == p.messages("MEMORY", rebound)
    messages = [dict(role="system", content="system"), dict(role="user", content="Current window:\nText.")]
    reader = FrozenReader([dict(call_id="old", raw_request=dict(messages=messages), parsed_output="Q1 | Text.")], None)
    with pytest.raises(AssertionError, match="window changed"):
        asyncio.run(reader.complete(interface="UPDATE", messages=[messages[0], dict(role="user", content="Current window:\nOther.")]))
    assert not reader.used
    assert asyncio.run(reader.complete(interface="UPDATE", messages=messages)) == "Q1 | Text."
    assert len(reader.used) == 1


def test_freezing_low_agent_extraction_does_not_freeze_recall():
    from scripts.evaluate_r2_memory_chain import FrozenReader
    class Live:
        async def complete(self, *, interface, messages, **kwargs):
            assert interface == "RECALL" and messages == ["live candidates"]
            return "SELECT | F1"
    reader = FrozenReader([], None, Live())
    assert asyncio.run(reader.complete(interface="RECALL", messages=["live candidates"])) == "SELECT | F1"
    assert reader.used == []
