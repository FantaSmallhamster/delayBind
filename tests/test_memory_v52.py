import json
import sqlite3

import pytest

from delaybind_core.archive import SentenceArchive
from delaybind_core.cursor_v52 import TextReadCursor
from delaybind_core.evidence_context import build_answer_context, build_memory_context
from delaybind_core.fact_protocol_v52 import UpdateV52, parse_update
from delaybind_core.memory_protocol_v52 import MemoryProposal
from delaybind_core.navigation_graph import assert_dual_graph_invariants
from delaybind_core.replay import replay_events
from delaybind_core.schema import parse_query_plan
from delaybind_core.schema_v52 import QueryPlanV3, use_key
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subqueries_v52 import SubqueryRuntime
from delaybind_core.token_budget import TokenCounter
from test_v52_sources import CharacterTokenizer


def plan_chain():
    return QueryPlanV3(plan_id="p", queries=[
        {"id": "Q1", "template": "Who teaches Cindy?", "output": "?teacher"},
        {"id": "Q2", "template": "Who is ?teacher's mother?", "output": "?mother", "inputs": {"?teacher": "Q1"}},
        {"id": "Q3", "template": "Where was ?mother born?", "output": "?city", "inputs": {"?mother": "Q2"}},
    ])


def runtime_for(text="Cindy's teacher is Alice. Alice's mother is Mary. Mary was born in Suzhou.", plan=None):
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    runtime = SubqueryRuntime(run_id="r", plan=plan or plan_chain(), archive=archive, store=store)
    cursor = TextReadCursor(text, archive, sample_id="s", counter=TokenCounter(CharacterTokenizer()))
    window = cursor.next_anchored_window(10000)
    runtime.record_window(window, cursor.state())
    return runtime


def ingest(runtime, qids, ref, text=None):
    update = UpdateV52(facts=[{"query_ids": qids, "source_refs": [ref],
                              "text": text or runtime.archive.fetch_sentence(ref).text}], hints=[])
    runtime.ingest_all(update, visible_sources=[ref], window_index=0)
    return next(f.fact_id for f in runtime.state.facts.values() if f.text == update.facts[0].text)


def assess(qid, fid, ref, verdict="ACCEPT", **kwargs):
    return dict(op="ASSESS", query_id=qid, fact_id=fid, verdict=verdict,
                checked_refs=[ref], reason_code="TEST_RAW_DECISION", **kwargs)


def bind(qid, fid, value="Alice", kind="DIRECT"):
    return dict(op="BIND", query_id=qid, value=value, kind=kind, support_fact_ids=[fid] if fid else [])


def apply(runtime, qid, ops, *, context=None):
    context = context or build_memory_context(runtime, qid)
    proposal = MemoryProposal(schema_version="memory-v5.2", context_id=context.context_id, operations=ops)
    runtime.apply_memory(proposal, context)
    return context, proposal


def select_all(runtime, qid):
    staged = runtime.state.model_copy(deep=True)
    for job in staged.recall_jobs.values():
        if job.query_id == qid and job.stage == "SCAN":
            job.stage = "REVIEW"
            job.scan_offset = len(job.candidate_ids)
            job.selected_fact_ids = list(job.candidate_ids)
            for fid in job.selected_fact_ids:
                staged.uses[use_key(qid, job.input_signature, fid)].status = "PENDING"
    runtime.commit(staged, [("TEST_SELECTION", {})])


def test_t01_pending_requires_raw_review_and_t05_empty_downstream_has_port():
    r = runtime_for()
    fid = ingest(r, ["Q1"], "D0:S0")
    assert next(iter(r.state.uses.values())).status == "PENDING"
    with pytest.raises(ValueError, match="PENDING_USE_NOT_ASSESSED"):
        apply(r, "Q1", [bind("Q1", fid)])
    apply(r, "Q1", [assess("Q1", fid, "D0:S0"), bind("Q1", fid)])
    assert r.state.bindings == {"?teacher": "Alice"}
    assert r.state.query_edges[0].binding_id == r.state.binding_ports[0].binding_id == "B1"
    assert not r.state.navigation_links
    assert len(r.state.facts) == 1
    assert_dual_graph_invariants(r.state)


def test_t04_reject_one_use_does_not_delete_shared_fact():
    plan = QueryPlanV3(plan_id="p", queries=[{"id": f"Q{i}", "template": f"Need {i}?", "output": f"?v{i}"} for i in (1, 2)])
    r = runtime_for(plan=plan)
    fid = ingest(r, ["Q1", "Q2"], "D0:S0")
    apply(r, "Q1", [assess("Q1", fid, "D0:S0", "REJECT")])
    apply(r, "Q2", [assess("Q2", fid, "D0:S0"), bind("Q2", fid)])
    assert len(r.state.facts) == 1
    assert sorted(u.status for u in r.state.uses.values()) == ["ACCEPTED", "REJECTED"]


def test_t06_correction_local_id_and_immutable_history():
    r = runtime_for()
    fid = ingest(r, ["Q1"], "D0:S0", "Cindy's teacher is Bob.")
    apply(r, "Q1", [assess("Q1", fid, "D0:S0", correction={"local_id": "N1", "text": "Cindy's teacher is Alice.",
                                                            "source_refs": ["D0:S0"]}), bind("Q1", "N1")])
    assert r.state.facts[fid].text.endswith("Bob.")
    corrected = next(f for f in r.state.facts.values() if f.supersedes_fact_id)
    assert corrected.supersedes_fact_id == fid
    assert r.state.binding_store["B1"].direct_fact_ids == [corrected.fact_id]
    assert "Alice" in r.archive.fetch_sentence("D0:S0").text


def test_t10_upstream_change_invalidates_uses_links_and_jobs():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    b = ingest(r, ["Q2"], "D0:S1")
    c = ingest(r, ["Q3"], "D0:S2")
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    select_all(r, "Q2")
    apply(r, "Q2", [assess("Q2", b, "D0:S1"), bind("Q2", b, "Mary")])
    select_all(r, "Q3")
    apply(r, "Q3", [assess("Q3", c, "D0:S2"), bind("Q3", c, "Suzhou")])
    assert len(r.state.navigation_links) == 2
    old_sig = r.state.executions["Q2"].input_signature
    # Runtime checks provenance and authority, not natural-language entailment.
    apply(r, "Q1", [bind("Q1", a, "Bob")])
    assert r.state.bindings == {"?teacher": "Bob"}
    assert all(not r.state.binding_store[f"B{i}"].valid for i in (1, 2, 3))
    assert r.state.uses[use_key("Q2", old_sig, b)].status == "INVALIDATED"
    assert not r.state.navigation_links
    assert len(r.state.facts) == 3
    assert any(j.stage == "INVALIDATED" for j in r.state.recall_jobs.values())


def test_t11_all_parent_revisions_are_in_input_signature():
    plan = QueryPlanV3(plan_id="p", queries=[
        {"id": "Q1", "template": "First?", "output": "?a"}, {"id": "Q2", "template": "Second?", "output": "?b"},
        {"id": "Q3", "template": "Relationship of ?a and ?b?", "output": "?c", "inputs": {"?a": "Q1", "?b": "Q2"}}])
    r = runtime_for(plan=plan)
    a = ingest(r, ["Q1", "Q2", "Q3"], "D0:S0")
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    assert r.state.executions["Q3"].status == "DORMANT" and not r.state.recall_jobs
    apply(r, "Q2", [assess("Q2", a, "D0:S0"), bind("Q2", a, "Mary")])
    signature = r.state.executions["Q3"].input_signature
    assert len([j for j in r.state.recall_jobs.values() if j.stage == "SCAN"]) == 1
    apply(r, "Q1", [bind("Q1", a, "Bob")])
    assert r.state.executions["Q3"].input_signature != signature


def test_t12_idempotent_delivery_and_t13_new_proof_revision():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    context, proposal = apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    revision = r.state.state_revision
    r.apply_memory(proposal, context)
    assert r.state.state_revision == revision and len(r.state.binding_store) == 1
    apply(r, "Q1", [bind("Q1", a)])
    assert len(r.state.binding_store) == 1
    a2 = ingest(r, ["Q1"], "D0:S0", "Alice teaches Cindy.")
    apply(r, "Q1", [assess("Q1", a2, "D0:S0"), bind("Q1", a2)])
    assert len(r.state.binding_store) == 2
    assert r.state.binding_store["B2"].binding_revision == 2


@pytest.mark.parametrize("verdict", ["HOLD", "CONFLICT"])
def test_t14_new_blocker_suspends_resolved_binding(verdict):
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    new = ingest(r, ["Q1"], "D0:S1")
    apply(r, "Q1", [assess("Q1", new, "D0:S1", verdict)])
    assert not r.state.bindings and r.state.executions["Q1"].status == "ACTIVE"
    pack = build_answer_context(r)
    assert any(n.get("fact_id") == new for n in pack.unresolved_or_conflicts)
    assert "D0:S1" in {raw.source_ref for raw in pack.raw_evidence}


def test_t20_invalid_operation_rolls_back_entire_memory():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    before = r.state.model_dump(mode="json")
    context = build_memory_context(r, "Q1")
    with pytest.raises(ValueError):
        apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q2", a)], context=context)
    assert r.state.model_dump(mode="json") == before
    assert r.store.latest_v52_state("r") == before


def test_t21_database_failure_and_truncated_replay(monkeypatch):
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    before = r.state.model_dump(mode="json")
    real = r.store._insert_v52_event
    count = 0

    def fail(db, event):
        nonlocal count
        count += 1
        if count == 2:
            raise sqlite3.OperationalError("injected")
        real(db, event)

    monkeypatch.setattr(r.store, "_insert_v52_event", fail)
    with pytest.raises(sqlite3.OperationalError):
        apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    assert r.state.model_dump(mode="json") == r.store.latest_v52_state("r") == before
    monkeypatch.setattr(r.store, "_insert_v52_event", real)
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    events = r.store.list_runtime_events("r")
    assert replay_events(events).model_dump() == r.state.model_dump()
    assert replay_events(events[:-1]).model_dump(mode="json") == before


def test_t22_restore_pending_job_with_read_watermark():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    ingest(r, ["Q2"], "D0:S1")
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    resumed = SubqueryRuntime(run_id="r", plan=r.state.plan, archive=SentenceArchive(r.store, "r"), store=r.store)
    assert resumed.state == r.state
    assert next(iter(resumed.state.recall_jobs.values())).stage == "SCAN"
    assert resumed.archive.watermark == r.state.read_watermark


def test_t23_patch_and_route_ready_query_schedules_without_new_binding():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a)])
    apply(r, "Q1", [{"op": "PATCH", "query_id": "Q4", "patch": {
        "template": "What else about Cindy?", "output": "?other", "inputs": {}, "cardinality": "SINGLE"},
        "evidence_fact_ids": [a]}, {"op": "ROUTE", "query_id": "Q4", "fact_ids": [a]}])
    assert r.state.executions["Q4"].status == "ACTIVE"
    assert any(j.query_id == "Q4" and j.stage == "SCAN" for j in r.state.recall_jobs.values())


def test_t24_complete_set_waits_for_eof_but_members_are_accepted():
    plan = QueryPlanV3(plan_id="p", queries=[{"id": "Q1", "template": "All teachers?", "output": "?teachers",
                                            "cardinality": "SET", "requires_complete_set": True}])
    r = runtime_for(plan=plan)
    a = ingest(r, ["Q1"], "D0:S0")
    apply(r, "Q1", [assess("Q1", a, "D0:S0")])
    with pytest.raises(ValueError, match="SCOPE_NOT_CLOSED"):
        apply(r, "Q1", [bind("Q1", a, ["Alice"])])
    r.close_scope()
    apply(r, "Q1", [bind("Q1", a, ["Alice"])])
    assert r.state.bindings["?teachers"] == ["Alice"]


def test_parent_and_initially_dormant_child_cannot_bind_together():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    b = ingest(r, ["Q2"], "D0:S1")
    with pytest.raises(ValueError, match="QUERY_NOT_BINDABLE"):
        apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a), bind("Q2", b, "Mary")])
    assert not r.state.bindings


def test_focus_pins_bridge_raw_and_inferred_binding_has_declared_parent_proof():
    r = runtime_for()
    a = ingest(r, ["Q1"], "D0:S0")
    apply(r, "Q1", [assess("Q1", a, "D0:S0"), bind("Q1", a), {"op": "FOCUS", "fact_ids": []}])
    context = build_memory_context(r, "Q2")
    assert "D0:S0" in context.allowed_source_refs
    apply(r, "Q2", [bind("Q2", None, "Mary", kind="INFERRED")], context=context)
    assert r.state.binding_store["B2"].source_refs == ["D0:S0"]
    assert len(r.state.facts) == 1


def test_update_partial_validation_and_explicit_plan_version():
    raw = "Q1 | D0:S0 | ok | ACTIVE\nQ1 | D0:S9 | future | ACTIVE"
    valid, errors = parse_update(raw, query_ids=["Q1"], visible_sources=["D0:S0"])
    assert len(valid.facts) == len(errors) == 1
    with pytest.raises(ValueError):
        parse_query_plan({"schema_version": "v2", "queries": []})


@pytest.mark.parametrize("queries", [
    [{"id": "Q1", "template": "Need ?missing", "output": "?v"}],
    [{"id": "Q1", "template": "Root", "output": "?v"}, {"id": "Q2", "template": "Other", "output": "?v"}],
    [{"id": "Q1", "template": "?b", "output": "?a", "inputs": {"?b": "Q2"}},
     {"id": "Q2", "template": "?a", "output": "?b", "inputs": {"?a": "Q1"}}],
])
def test_plan_rejects_missing_inputs_duplicates_and_cycles(queries):
    with pytest.raises(ValueError):
        QueryPlanV3(plan_id="bad", queries=queries)


@pytest.mark.parametrize("raw,summary,reason", [
    ("Alice did not teach Cindy.", "Alice taught Cindy.", "NEGATION_MISMATCH"),
    ("Cindy taught Alice.", "Alice taught Cindy.", "RELATION_DIRECTION_MISMATCH"),
    ("Alice taught Cindy in 1990.", "Alice taught Cindy in 2000.", "TIME_SCOPE_MISMATCH"),
    ("A different Cindy was taught by Alice.", "This Cindy was taught by Alice.", "IDENTITY_SCOPE_MISMATCH"),
])
def test_t07_t09_summary_does_not_bypass_source_review(raw, summary, reason):
    r = runtime_for(text=raw)
    fid = ingest(r, ["Q1"], "D0:S0", summary)
    context = build_memory_context(r, "Q1")
    assert context.working_memory.raw_evidence[0].text == raw
    with pytest.raises(ValueError, match="PENDING_USE_NOT_ASSESSED"):
        apply(r, "Q1", [bind("Q1", fid)], context=context)
    op = assess("Q1", fid, "D0:S0", "REJECT")
    op["reason_code"] = reason
    apply(r, "Q1", [op], context=context)
    assert not r.state.bindings and fid in r.state.facts


def test_fragment_hold_is_requeued_when_complete_source_arrives():
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    r = SubqueryRuntime(run_id="r", plan=plan_chain(), archive=archive, store=store)
    cursor = TextReadCursor("Cindy's teacher is Alice, not Bob.", archive, sample_id="s",
                            counter=TokenCounter(CharacterTokenizer()), window_mode="fragment")
    first = cursor.next_anchored_window(25)
    r.record_window(first, cursor.state())
    ref = first.source_refs[0]
    fid = ingest(r, ["Q1"], ref)
    with pytest.raises(ValueError, match="INCOMPLETE_SOURCE"):
        apply(r, "Q1", [assess("Q1", fid, ref)])
    apply(r, "Q1", [assess("Q1", fid, ref, "HOLD")])
    while not cursor.exhausted:
        window = cursor.next_anchored_window(25)
        r.record_window(window, cursor.state())
    context = build_memory_context(r, "Q1")
    assert context.pending_uses == [{"query_id": "Q1", "fact_id": fid}]
    assert "D0:S0" in context.allowed_source_refs
    op = assess("Q1", fid, ref, correction={"local_id": "N1", "text": "Cindy's teacher is Alice.", "source_refs": ["D0:S0"]})
    op["checked_refs"].append("D0:S0")
    apply(r, "Q1", [op, bind("Q1", "N1")], context=context)
    assert r.state.bindings == {"?teacher": "Alice"}
