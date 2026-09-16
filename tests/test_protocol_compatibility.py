import asyncio
import json
import re

import pytest

from delaybind_core.fact_protocol import (
    ProtocolError, normalize_memory_queries, parse_judgments, parse_memory,
    parse_selection, parse_update, resolve_query_id,
)
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema import QueryPlan
from delaybind_core.storage import SQLiteEventStore


@pytest.mark.parametrize("raw", [
    "SELECT | F1,F2", "SELECT F1,F2", "SELECT|F1, F2", "select: F1,F2",
    'SELECT ["F1", "F2"]', '["F1", "F2"]', "F1,F2", "F1\nF2",
    "fact_id1, fact_id2\n\nF1,F2", "```text\nSELECT F1,F2\n```",
    "- SELECT `F1`, `F2`", "SELECT [F1,F2]",
])
def test_selection_format_variants_match_the_same_candidate_set(raw):
    assert parse_selection(raw, {"F1", "F2"}) == {"F1", "F2"}


@pytest.mark.parametrize("raw", ["NONE", "none", "SELECT NONE", "SELECT | NONE", "SELECT []", "[]"])
def test_empty_selection_variants(raw):
    assert parse_selection(raw, {"F1"}) == set()


def test_selecting_known_context_cannot_send_active_facts_to_verify():
    ignored = set()
    assert parse_selection("SELECT F1,Fparent", {"F1"}, context_ids={"Fparent"}, ignored_context=ignored) == {"F1"}
    assert ignored == {"Fparent"}
    assert parse_selection("SELECT Fparent", {"F1"}, context_ids={"Fparent"}) == set()
    for raw in ["SELECT F1,Ftypo", "I select F1 because it is relevant", "NONE,F1"]:
        with pytest.raises(ProtocolError):
            parse_selection(raw, {"F1"}, context_ids={"Fparent"})


@pytest.mark.parametrize("qid", ["Q1@NONE", "q1@none", "q1", "`Q1`", '"Q1"'])
def test_only_declared_query_ids_can_be_normalized(qid):
    assert resolve_query_id(qid, {"Q1", "Q2"}) == "Q1"
    with pytest.raises(ProtocolError):
        resolve_query_id("Q3@NONE", {"Q1", "Q2"})
    with pytest.raises(ProtocolError):
        resolve_query_id("Q1@Alice", {"Q1"})


def test_memory_json_references_notes_and_missing_support_are_independent():
    raw = '''BIND | Q1@NONE | "1971" | ["F1", "F2"]
BIND Q2 | 2007 | NONE
KEEP | ["F1", "F2"]
NOTE:
Q1 is supported by the two facts.
BIND|Q3|false|F2
'''
    response = normalize_memory_queries(parse_memory(raw, recover=True), {"Q1", "Q2", "Q3"})
    assert [(binding.query_id, binding.value, binding.support_refs) for binding in response.bindings] == [
        ("Q1", "1971", ["F1", "F2"]), ("Q3", False, ["F2"]),
    ]
    assert response.keep == ["F1", "F2"]
    assert len(response.rejected_lines) == 1
    assert response.ignored_lines


def test_bad_update_rows_do_not_erase_supported_rows():
    update = parse_update('''Q1|D18@C0|Gary is a member of Snow Patrol.|ACTIVE
Q2|NONE|No source states another membership.|DORMANT
Q3|NONE|
''', recover=True)
    assert len(update.facts) == 1
    assert update.facts[0].source_refs == ["D18@C0"]
    assert len(update.rejected_lines) == 1
    assert update.ignored_lines[0]["reason"] == "NO_EVIDENCE_REPORT"
    with pytest.raises(ProtocolError):
        parse_update("Q1 | D18 | fact | MAYBE")


@pytest.mark.parametrize("raw", ["F1 | MATCH", "F1 MATCH", "F1: match", "`F1` | MATCH", "F1 MATCH\nF1 MATCH"])
def test_verify_accepts_only_unambiguous_complete_judgments(raw):
    assert parse_judgments(raw, {"F1"}) == {"F1": "MATCH"}
    for invalid in ["F1 | MATCH\nF1 | MISMATCH", "F2 | MATCH", "F1 | MATCH\nF2 | MATCH"]:
        with pytest.raises(ProtocolError):
            parse_judgments(invalid, {"F1"})


FIRST = "Document 44:\nAlice\nAlice's mother is Mary.\n\n"
SECOND = "Document 45:\nCindy\nCindy's teacher is Alice."


def chain_plan():
    return QueryPlan(plan_id="chain", queries=[
        {"id": "Q1", "template": "Who is Cindy's teacher?", "output": "?teacher"},
        {"id": "Q2", "template": "Who is ?teacher's mother?", "output": "?mother", "depends_on": ["Q1"]},
    ])


def facts_in(text):
    return {line.split(" | ", 2)[0]: line.split(" | ", 2)[2]
            for line in text.splitlines() if re.match(r"^F[0-9a-f]+ \|", line)}


class CompatibilityClient:
    def __init__(self, bad_interface=None):
        self.bad_interface = bad_interface
        self.calls = []
        self.parent_id = None
        self.candidate_id = None

    async def complete(self, *, interface, messages, **kwargs):
        self.calls.append(interface)
        prompt = messages[0]["content"]
        if interface == "UPDATE":
            if "[D44@C0]" in prompt:
                return "Q2@NONE|doc_44|Alice's mother is Mary.|DORMANT"
            return "q1|Document 45|Cindy's teacher is Alice.|ACTIVE"
        if interface == "MEMORY":
            memory = prompt.split("Working memory:\n", 1)[1].split("\n\nSaved plan hints:", 1)[0]
            facts = facts_in(memory)
            self.parent_id = next(fid for fid, text in facts.items() if "Cindy's teacher" in text)
            if "Q1 [ACTIVE]" in prompt:
                # This premature child proposal must remain unbound until review.
                return (f'BIND Q1@NONE | Alice | ["{self.parent_id}"]\n'
                        f'BIND | Q2 | WRONG | {self.parent_id}\nNOTE:\nThe child still needs its evidence.')
            mother = next((fid for fid, text in facts.items() if "Alice's mother" in text), None)
            return f'BIND | q2 | Mary | ["{mother}"]' if mother else "NONE"
        if interface == "RECALL":
            candidates = facts_in(prompt.split("Deferred candidates:\n", 1)[1].split("\n\nReturn SELECT", 1)[0])
            self.candidate_id = next(iter(candidates))
            if self.bad_interface == "RECALL":
                return "SELECT Ftypo"
            return f"SELECT {self.candidate_id}, {self.parent_id}"
        if interface == "VERIFY":
            candidates = facts_in(prompt.split("Selected deferred candidates:\n", 1)[1].split("\n\nTheir already-read", 1)[0])
            assert set(candidates) == {self.candidate_id}
            if self.bad_interface == "VERIFY":
                return "Ftypo MATCH"
            return f"{self.candidate_id} MATCH"
        if interface == "ANSWER":
            return r"\boxed{unknown}" if self.bad_interface else r"\boxed{Mary}"
        raise AssertionError(interface)


def run_chain(client):
    store = SQLiteEventStore()
    result = asyncio.run(V5Runner(client, config=RunnerConfig(chunk_size=len(FIRST))).run(
        run_id="compatibility", question="Who is Cindy's teacher's mother?", context=FIRST + SECOND,
        plan=chain_plan(), store=store,
    ))
    return result, store.list_runtime_events("compatibility")


def test_format_variants_complete_binding_recall_verify_without_early_child_binding():
    client = CompatibilityClient()
    result, events = run_chain(client)
    assert result["state"]["bindings"] == {"?teacher": "Alice", "?mother": "Mary"}
    assert result["protocol_valid"]
    assert client.calls.count("RECALL") == client.calls.count("VERIFY") == 1
    assert any(event.event_type == "RECALL_CONTEXT_IDS_IGNORED" for event in events)
    assert any(event.event_type == "BINDING_HELD" for event in events)
    child_bindings = [event for event in events if event.event_type == "QUERY_BOUND" and event.payload["query_id"] == "Q2"]
    assert len(child_bindings) == 1 and child_bindings[0].payload["value"] == "Mary"
    promotion = next(event for event in events if event.event_type == "FACT_PROMOTED")
    assert promotion.event_seq < child_bindings[0].event_seq


@pytest.mark.parametrize("interface", ["RECALL", "VERIFY"])
def test_exhausted_repairs_cannot_promote_invalid_ids_and_do_not_abort_answer(interface):
    client = CompatibilityClient(interface)
    result, events = run_chain(client)
    assert not result["protocol_valid"]
    assert result["protocol_repair_failures"][0]["interface"] == interface
    assert result["state"]["bindings"] == {"?teacher": "Alice"}
    assert not any(event.event_type == "FACT_PROMOTED" for event in events)
    assert client.calls.count(interface) == 2
    assert client.calls[-1] == "ANSWER"
    assert client.calls.count("VERIFY") == (0 if interface == "RECALL" else 2)
    if interface == "VERIFY":
        verdict = next(event.payload for event in events if event.event_type == "CANDIDATE_REVIEWED")
        assert verdict["verdict"] == "UNCERTAIN" and verdict["verdict_source"] == "PROTOCOL_FALLBACK"


@pytest.mark.parametrize("interface", ["UPDATE", "MEMORY"])
def test_invalid_rows_after_repair_preserve_valid_facts_and_supported_bindings(interface):
    class Client:
        calls = []

        async def complete(self, *, interface: str, messages, **kwargs):
            self.calls.append(interface)
            if interface == "UPDATE":
                good = "Q1@NONE | D44 | Alice's mother is Mary. | ACTIVE"
                return good + ("\nQ1 | NONE | Unsupported fact. | DORMANT" if self.bad == "UPDATE" else "")
            if interface == "MEMORY":
                fid = next(iter(facts_in(messages[0]["content"])))
                good = f'BIND|Q1|Mary|["{fid}"]'
                return good + ("\nBIND | UNKNOWN | missing | NONE" if self.bad == "MEMORY" else "")
            if interface == "ANSWER":
                return r"\boxed{Mary}"
            raise AssertionError(interface)

    client = Client()
    client.bad = interface
    client.calls = []
    plan = QueryPlan(plan_id="one", queries=[{"id": "Q1", "template": "Who is Alice's mother?", "output": "?mother"}])
    store = SQLiteEventStore()
    result = asyncio.run(V5Runner(client).run(run_id="partial", question="Who is Alice's mother?",
        context=FIRST, plan=plan, store=store))
    assert result["state"]["bindings"] == {"?mother": "Mary"}
    assert len(result["state"]["working_memory"]["facts"]) == 1
    assert result["evidence_pack"]["answer_value"] == "Mary"
    assert not result["protocol_valid"]
    assert client.calls.count(interface) == 2
    assert result["protocol_repair_failures"][0]["interface"] == interface
