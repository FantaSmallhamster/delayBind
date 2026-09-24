"""The member MEMORY contract separates state descriptions and output commands."""

import asyncio

import pytest

from delaybind_core.memory_repair_hints_r2 import fact_only_repair_hint
from delaybind_core.prompts_r2 import (
    MEMBER_MEMORY_PROMPT_VERSION,
    MEMBER_PROMPT_VERSION,
    MEMBER_UPDATE_PROMPT_VERSION,
    messages,
    prompt_version_for,
    request_view,
)
from delaybind_core.protocol_r2 import parse_memory
from delaybind_core.runner import RunnerConfig
from delaybind_core.smoke_r2 import ScriptedR2Client, run_smoke
from test_r2_core import context, recall_all
from test_r2_member_graph import decide, ingest, runtime, seeded


def payload(*, empty=False, old=False):
    return dict(
        question="Where were Team Red's members born?", fact_only=True,
        member_bindings=True, phase="FINAL", allowed_mode="REBIND" if old else "BIND",
        allowed_fact_ids=[] if empty else ["fact-a"],
        query_instance=dict(id="Q2", template="Who are the members of ?team?",
                            rendered_query="Who are the members of Team Red?",
                            output="?person", bound_inputs={} if empty else {"?team": "Team Red"}),
        working_memory={"navigation": {"facts": [] if empty else [
            dict(fact_id="fact-a", text="Team Red includes Alice.")]}},
        old_binding=dict(value="Alice", members=[dict(value="Alice", direct_fact_ids=["fact-a"])]) if old else None,
    )


def repair(raw, error, original=None):
    return dict(original_memory_request=original or payload(), rejected_response=raw,
                validation_errors=error)


def test_empty_input_markers_do_not_look_like_output_commands():
    text = messages("MEMORY", payload(empty=True))[1]["content"].split("Input data:\n", 1)[1]
    assert "Eligible facts:\nNo eligible facts" in text
    assert "Existing binding:\nNo existing binding" in text
    assert "Effective upstream bindings:\nNo upstream bindings" in text
    assert "NONE" not in text and "cardinality" not in text
    assert "Who are the members of Team Red?" in text


@pytest.mark.parametrize("interface", ["MEMORY", "MEMORY_REPAIR"])
def test_fact_only_member_memory_omits_global_question(interface):
    data = payload()
    request = data if interface == "MEMORY" else repair("NOOP", "invalid response", data)
    contents = [message["content"] for message in messages(interface, request)]
    assert all("Where were Team Red's members born?" not in content for content in contents)
    view = contents[-1].split("Input data:\n", 1)[1]
    assert "Question:\n" not in view
    assert "Current query:\nQ2 | Who are the members of Team Red?\noutput=?person" in view
    assert "Effective upstream bindings:\n?team=Team Red" in view


def test_previous_members_are_labeled_not_two_field_commands():
    text = messages("MEMORY", payload(old=True))[1]["content"].split("Input data:\n", 1)[1]
    assert "Existing binding:\nvalue=Alice; support=F1" in text
    assert "Alice | F1" not in text and "fact-a" not in text
    assert "Mode:\nREBIND" in text
    assert "?team=Team Red" in text


def test_legacy_nonmember_empty_input_is_unchanged():
    legacy = payload(empty=True)
    legacy["member_bindings"] = False
    text = request_view("MEMORY", legacy)
    assert "Eligible facts:\nNONE" in text and "Existing binding:\nNONE" in text


def test_new_version_changes_only_fact_only_member_memory():
    data = payload()
    for interface in ("MEMORY", "MEMORY_REPAIR"):
        p = data if interface == "MEMORY" else repair("Alice | F1", "bad")
        assert prompt_version_for(interface, p) == MEMBER_MEMORY_PROMPT_VERSION
    assert prompt_version_for("UPDATE", data) == MEMBER_UPDATE_PROMPT_VERSION
    from delaybind_core.prompts_r2 import ANSWER_PROMPT_VERSION
    assert prompt_version_for("ANSWER", data) == ANSWER_PROMPT_VERSION
    assert prompt_version_for("MEMORY", {**data, "fact_only": False}) == MEMBER_PROMPT_VERSION


def test_binding_contract_matches_current_hop_and_preserves_rebind():
    text = messages("MEMORY", payload())[1]["content"]
    assert "BIND: Return all independently supported, compatible values for this hop." in text
    assert "Do not wait for later" in text
    assert "unambiguous paraphrases and inverse" in text
    assert "Query: Who is Mira's child?" in text
    assert "F1 | Rowan's mother is Mira." in text
    assert "BOUND | Rowan | F1" in text
    assert "REBIND: Without an existing binding, use BIND." in text
    assert "NOOP neither rejects candidates nor removes bindings." in text


def test_missing_bound_prefix_gets_specific_feedback_but_is_not_auto_bound():
    r = runtime()
    ingest(r, ("Q1", "Jane leads Team Red."))
    ctx = context(r, "Q1")
    before = r.export()
    with pytest.raises(ValueError) as error:
        parse_memory("Team Red | F1", ctx)
    data = repair("Team Red | F1", str(error.value))
    text = messages("MEMORY_REPAIR", data)[1]["content"]
    assert "Line 1: received 2 fields" in text
    assert "required leading BOUND field is missing" in text
    assert str(error.value) in text
    assert "Rejected response (untrusted):\nTeam Red | F1" in text
    assert "Current query:\nQ2 | Who are the members of Team Red?" in text
    assert "format error does not by itself justify" in text
    assert r.export() == before


@pytest.mark.parametrize("raw", ["Alice | F999", "Alice | NONE", "Alice |", "REVIEW | F1"])
def test_ambiguous_rows_are_not_described_as_safe_missing_prefix_repairs(raw):
    hint = fact_only_repair_hint(repair(raw, "INVALID_FACT_ONLY_MEMORY_FIELD"))
    assert "leading BOUND field is missing" not in hint


@pytest.mark.parametrize(("raw", "error", "detail"), [
    ("BOUND | Alice", "FACT_ONLY_BOUND_FIELD_COUNT:expected=3,received=2", "BOUND has 2 fields; exactly 3"),
    ("BOUND | Alice | F1 | reason", "FACT_ONLY_BOUND_FIELD_COUNT:expected=3,received=4", "BOUND has 4 fields; exactly 3"),
    ("BOUND | Alice | F99", "UNKNOWN_FACT_ALIAS:F99", "use only IDs displayed"),
    ("BOUND | Alice | NONE", "MISSING_MEMBER_PROOF", "Replace the NONE support field"),
    ("NOOP\nBOUND | Alice | F1", "MEMBER_MEMORY_EXPECTS_BOUND_LINES_OR_ONE_NOOP", "Do not combine"),
    ("NONE\nBOUND | Alice | F1", "STANDALONE_NONE_CANNOT_BE_MIXED_WITH_OTHER_MEMORY_LINES", "Do not combine"),
])
def test_repair_feedback_is_specific(raw, error, detail):
    assert detail in fact_only_repair_hint(repair(raw, error))


def test_escaped_pipe_inside_value_is_one_field_in_repair_feedback():
    hint = fact_only_repair_hint(repair(r"Research \| Development | F1", "INVALID_FACT_ONLY_MEMORY_FIELD"))
    assert "received 2 fields" in hint


def test_unbound_branch_in_rebind_uses_initial_binding_rule():
    r = runtime()
    *_, paris, rome, _, _ = seeded(r)
    # Publish Alice's city while Bob's branch remains unresolved.
    recall_all(r, "Q3")
    for _ in range(2):
        ctx = context(r, "Q3")
        if ctx.branch.bound_inputs["?person"] == "Alice":
            decide(r, "Q3", ("Paris", [paris]))
        else:
            decide(r, "Q3", raw="NOOP")
    ingest(r, ("Q3", "Bob's birthplace is Rome."))
    for _ in range(2):
        ctx = context(r, "Q3")
        if ctx.branch.bound_inputs["?person"] == "Bob":
            data = payload()
            data.update(allowed_mode=ctx.allowed_mode, old_binding=None)
            text = messages("MEMORY", data)[1]["content"]
            assert ctx.allowed_mode == "REBIND"
            assert "No existing binding" in text
            assert "REBIND: Without an existing binding, use BIND." in text
            decide(r, "Q3", ("Rome", [rome]))
        else:
            decide(r, "Q3", raw="NOOP")
    current = r.state.binding_store[r.state.executions["Q3"].current_binding_id]
    assert {member.value for member in current.members} == {"Paris", "Rome"}


def test_runner_sends_actionable_prefix_repair_for_same_snapshot():
    class MissingPrefix(ScriptedR2Client):
        failed = None

        async def complete(self, **kwargs):
            raw = await super().complete(**kwargs)
            if kwargs["interface"] == "MEMORY" and self.failed is None and raw.startswith("BOUND | "):
                self.failed = kwargs["local_metadata"]
                return raw.removeprefix("BOUND | ")
            if kwargs["interface"] == "MEMORY_REPAIR":
                assert kwargs["local_metadata"] == self.failed
                prompt = kwargs["messages"][-1]["content"]
                assert "required leading BOUND field is missing" in prompt
                assert "rejected reply has not changed runtime state" in prompt
            return raw

    result = asyncio.run(run_smoke(client=MissingPrefix(), config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False, chunk_size=1000)))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["interface_calls"]["MEMORY_REPAIR"] == 1
    assert result["answer"]["answer"] == "Suzhou"
