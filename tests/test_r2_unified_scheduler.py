"""Unified evidence scheduling, historical candidates, repair routes and replay."""

from dataclasses import replace

import pytest

from delaybind_core.context_r2 import build_memory_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.navigation_r2 import assert_invariants, query_ready, render_query
from delaybind_core.plan_repair_r2 import apply_repair, build_repair_context
from delaybind_core.protocol_r2 import parse_memory
from delaybind_core.replay import replay_events
from delaybind_core.review_jobs_r2 import next_job, schedule_ready
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import EvidencePlanR2, StateR2
from test_r2_core import fixture, context
from test_r2_member_graph import ingest


def runtime(plan=None):
    plan = plan or EvidencePlanR2(plan_id="unified", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who is ?teacher's mother?", output="?mother", inputs={"?teacher": "Q1"}),
        dict(id="Q3", template="Who leads Team Blue?", output="?leader"),
    ])
    return fixture(plan=plan, config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False,
        memory_interface="unified_evidence_v1", plan_repair_mode="on_hint"))


def decide(r, qid, *members):
    ctx = context(r, qid)
    aliases = {fid: alias for alias, fid in ctx.fact_aliases.items()}
    raw = "\n".join(f"{qid} | {','.join(aliases[fid] for fid in facts) or 'NONE'} | {value}"
                    for value, facts in members) or f"{qid} | NONE | UNKNOWN"
    response = parse_memory(raw, ctx)
    receipt = apply_memory(r, response, ctx)
    assert_invariants(r.state)
    return ctx, response, receipt


def reschedule(r, qid):
    state, events = r.state.model_copy(deep=True), []
    schedule_ready(state, qid, "TEST_REPEAT", events, force_review=True)
    r.commit(state, events)


def test_dormant_candidates_wait_then_wake_without_another_update_or_recall_call():
    r = runtime()
    mother, = ingest(r, ("Q2", "Bob's mother is Mary."))
    assert not query_ready(r.state, "Q2")
    assert next_job(r.state) is None
    teacher, = ingest(r, ("Q1", "Cindy's teacher is Bob."))
    decide(r, "Q1", ("Bob", [teacher]))
    assert next_job(r.state).target_query == "Q2"
    assert next_job(r.state).kind == "MEMORY"
    ctx = context(r, "Q2")
    assert render_query(r.state.plan.queries[1].template, ctx.branch.bound_inputs) == "Who is Bob's mother?"
    assert ctx.allowed_fact_ids == [mother]
    assert ctx.evidence_origins == ["DEFERRED"]
    assert r.state.reviews[ctx.review_id].admitted_use_tokens == {}
    assert not any(job.kind == "RECALL" for job in r.state.jobs.values())
    decide(r, "Q2", ("Mary", [mother]))
    assert r.export()["bindings"]["?mother"] == "Mary"


def test_unknown_keeps_history_for_joint_support_and_identical_evidence_does_not_loop():
    r = runtime()
    alias, = ingest(r, ("Q1", "Alias P7 names Bob."))
    decide(r, "Q1")
    count = len(r.state.reviews)
    for _ in range(3):
        reschedule(r, "Q1")
        assert next_job(r.state) is None
    ingest(r, ("Q1", "Alias P7 names Bob."))  # duplicate occurrence is not new evidence
    assert len(r.state.reviews) == count
    unrelated, = ingest(r, ("Q3", "Team Blue is led by Dana."))
    decide(r, "Q3", ("Dana", [unrelated]))
    assert len([rv for rv in r.state.reviews.values() if rv.query_id == "Q1"]) == count
    teacher, = ingest(r, ("Q1", "Cindy's teacher is P7."))
    ctx = context(r, "Q1")
    assert set(ctx.allowed_fact_ids) == {alias, teacher}
    assert set(ctx.evidence_origins) == {"DEFERRED", "UPDATE"}
    decide(r, "Q1", ("Bob", [alias, teacher]))
    assert r.export()["bindings"]["?teacher"] == "Bob"
    assert next_job(r.state) is None  # Q2 has no evidence; Q1 cannot ask itself again


def test_rebind_loads_old_support_unused_history_and_new_fact_without_tokens():
    r = runtime(EvidencePlanR2(plan_id="children", queries=[
        dict(id="Q1", template="Who are Mira's children?", output="?child")]))
    old, alias = ingest(r, ("Q1", "Mira's child is Rowan."), ("Q1", "Taylor and P7 name the same person."))
    decide(r, "Q1", ("Rowan", [old]))
    fresh, = ingest(r, ("Q1", "P7 is Mira's child."))
    ctx = context(r, "Q1")
    assert ctx.allowed_mode == "REBIND"
    assert set(ctx.allowed_fact_ids) == {old, alias, fresh}
    assert set(ctx.evidence_origins) == {"PRIOR_SUPPORT", "DEFERRED", "UPDATE"}
    assert {f["fact_id"] for f in ctx.working_memory.navigation["facts"]} == set(ctx.allowed_fact_ids)
    decide(r, "Q1", ("Rowan", [old]), ("Taylor", [alias, fresh]))
    binding = r.state.binding_store[r.state.executions["Q1"].current_binding_id]
    assert {m.value for m in binding.members} == {"Rowan", "Taylor"}
    assert set(r.state.facts) == {old, alias, fresh}
    assert all(use.admission_token is None for use in r.state.uses.values())


def test_new_evidence_cancels_frozen_session_even_while_ancestor_is_under_review():
    r = runtime()
    teacher, mother = ingest(r, ("Q1", "Cindy's teacher is Bob."), ("Q2", "Bob's mother is Mary."))
    decide(r, "Q1", ("Bob", [teacher]))
    old_ctx = context(r, "Q2")
    new_teacher, new_mother = ingest(r, ("Q1", "Cindy studied with Bob."), ("Q2", "Bob's mother Mary lived in Rome."))
    assert r.state.reviews[old_ctx.review_id].status == "CANCELLED"
    assert not query_ready(r.state, "Q2")
    with pytest.raises(ValueError, match="REVIEW_NOT_READY"):
        build_memory_context(r, old_ctx.review_id)
    decide(r, "Q1", ("Bob", [teacher]))  # unchanged proof releases the parent barrier
    ctx = context(r, "Q2")
    assert set(ctx.allowed_fact_ids) == {mother, new_mother}
    assert new_teacher not in ctx.allowed_fact_ids
    decide(r, "Q2", ("Mary", [mother]))


def test_plan_repair_new_route_wakes_memory_and_semantic_patch_invalidates_old_session():
    r = runtime()
    fact, = ingest(r, ("Q3", "Bob's mother is Mary."))
    ctx = build_repair_context(r, "Global question")
    apply_repair(r, f"REPAIR | {fact}\nUPSERT | Q4\nquery: Who is Bob's mother?\n"
                 f"output: ?other_mother\ndepends_on: NONE\nEND UPSERT\n"
                 f"ROUTE | Q4 | {fact}\nEND REPAIR", ctx)
    original = context(r, "Q4")
    assert original.allowed_fact_ids == [fact]
    assert original.evidence_origins == ["PLAN_REPAIR"]
    ctx = build_repair_context(r, "Global question")
    apply_repair(r, f"REPAIR | {fact}\nUPSERT | Q4\nquery: Who is Mary a mother of?\nEND UPSERT\nEND REPAIR", ctx)
    assert r.state.reviews[original.review_id].status == "CANCELLED"
    revised = context(r, "Q4")
    assert revised.query_version == original.query_version + 1
    assert revised.evidence_digest != original.evidence_digest
    assert revised.allowed_fact_ids == [fact]
    decide(r, "Q4", ("Bob", [fact]))
    assert not any(job.kind == "RECALL" for job in r.state.jobs.values())


def test_resume_is_idempotent_and_interfaces_cannot_be_switched():
    r = runtime()
    fact, = ingest(r, ("Q1", "Cindy's teacher is Bob."))
    ctx, response, receipt = decide(r, "Q1", ("Bob", [fact]))
    restored = RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive, store=r.store, config=r.config)
    assert apply_memory(restored, response, ctx) == receipt
    assert replay_events(r.store.list_runtime_events(r.run_id)).export() == restored.export()
    with pytest.raises(ValueError, match="RESUME_MEMORY_INTERFACE_CHANGED"):
        RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive, store=r.store,
                  config=replace(r.config, memory_interface="legacy_bind_rebind_v1"))


def test_plan_repair_route_to_bound_child_waits_for_parent_then_wakes():
    r = runtime()
    teacher, mother, history = ingest(r, ("Q1", "Cindy's teacher is Bob."),
                                     ("Q2", "Bob's mother is Mary."),
                                     ("Q3", "Bob's mother is also known as Maria."))
    decide(r, "Q1", ("Bob", [teacher]))
    decide(r, "Q2", ("Mary", [mother]))
    ingest(r, ("Q1", "Cindy studies mathematics with Bob."))
    assert not query_ready(r.state, "Q2")
    repair = build_repair_context(r, "Global question")
    apply_repair(r, f"REPAIR | {history}\nROUTE | Q2 | {history}\nEND REPAIR", repair)
    assert not any(rv.query_id == "Q2" and rv.status not in {"DONE", "CANCELLED"}
                   for rv in r.state.reviews.values())
    decide(r, "Q1", ("Bob", [teacher]))
    ctx = context(r, "Q2")
    assert ctx.allowed_mode == "REBIND"
    assert set(ctx.allowed_fact_ids) == {mother, history}
    decide(r, "Q2", ("Mary", [mother]))


def test_scope_closure_invalidates_complete_set_session_blocked_by_parent_review():
    plan = EvidencePlanR2(plan_id="complete", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who are ?teacher's children?", output="?child",
             inputs={"?teacher": "Q1"}, requires_complete_set=True),
    ])
    r = runtime(plan)
    teacher, child = ingest(r, ("Q1", "Cindy's teacher is Bob."), ("Q2", "Bob's child is Alice."))
    decide(r, "Q1", ("Bob", [teacher]))
    previous = context(r, "Q2")
    ingest(r, ("Q1", "Bob teaches mathematics."))
    assert not query_ready(r.state, "Q2")
    r.close_scope()
    assert r.state.reviews[previous.review_id].status == "CANCELLED"
    decide(r, "Q1", ("Bob", [teacher]))
    closed = context(r, "Q2")
    assert closed.scope_closed and closed.evidence_digest != previous.evidence_digest
    decide(r, "Q2", ("Alice", [child]))


def test_legacy_state_without_new_fields_still_reads_and_replays():
    r = fixture()
    old = r.state.model_dump(mode="json")
    old.pop("memory_interface")
    assert StateR2.model_validate(old).memory_interface == "legacy_bind_rebind_v1"
    # A transaction from before this interface existed is still a valid replay.
    events = r.store.list_runtime_events(r.run_id)
    for event in events:
        if event.event_type == "R2_TRANSACTION_COMMITTED":
            event.payload["state"].pop("memory_interface", None)
    assert replay_events(events).export() == r.export()
    with pytest.raises(ValueError, match="UNIFIED_MEMORY_REQUIRES_FACT_ONLY_MEMBER_PLAN"):
        RuntimeR2(run_id="bad", plan=r.state.plan, archive=r.archive, store=r.store,
                  config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                                      memory_interface="unified_evidence_v1"))
