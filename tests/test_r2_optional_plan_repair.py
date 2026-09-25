import sqlite3

import pytest

from delaybind_core.context_r2 import build_update_context
from delaybind_core.plan_repair_r2 import (
    PlanRepairProposalError, apply_repair, build_repair_context,
    record_plan_repair_rejection, repair_basis_key,
)
from delaybind_core.protocol_r2 import parse_update
from delaybind_core.replay import replay_events
from delaybind_core.runner import RunnerConfig
from test_r2_core import fixture


def hinted_runtime():
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          plan_repair_mode="on_hint",
                          plan_repair_failure_policy="continue_valid_plan")
    runtime = fixture(config=config)
    update_ctx = build_update_context(runtime, "Question?", ["D0:S0"])
    update, rejected = parse_update("PLAN_HINT | A new alias relation is needed.", update_ctx)
    assert rejected == []
    runtime.ingest(update, update_ctx)
    return runtime


def test_proposal_errors_are_local_and_rejection_keeps_business_state():
    runtime = hinted_runtime()
    ctx = build_repair_context(runtime, "Question?", persist=False)
    key = repair_basis_key(runtime.state, ctx)
    assert not any(m.get("interface") == "PLAN_REPAIR_SNAPSHOT"
                   for m in runtime.store.list_context_manifests(runtime.run_id))
    from delaybind_core.plan_repair_r2 import register_repair_context
    register_repair_context(runtime, ctx)
    before = runtime.state.model_copy(deep=True)
    with pytest.raises(PlanRepairProposalError) as error:
        apply_repair(runtime, "unparseable model proposal", ctx)
    assert error.value.code == "PLAN_REPAIR_FORMAT"
    assert runtime.state == before
    record_plan_repair_rejection(runtime, ctx, key, [error.value.code], ["response-hash"])
    after = runtime.state
    assert after.plan == before.plan
    assert after.executions == before.executions
    assert after.facts == before.facts
    assert after.jobs == before.jobs
    assert after.hints == before.hints and after.processed_hints == before.processed_hints
    assert after.cursor_state == before.cursor_state and after.pending_window == before.pending_window
    assert after.state_revision == before.state_revision + 1
    assert after.plan_repair_failures[key].attempt_count == 1
    assert repair_basis_key(after, build_repair_context(runtime, "Question?", persist=False)) == key
    assert replay_events(runtime.store.list_runtime_events(runtime.run_id)).export() == runtime.export()


def test_unregistered_stale_and_commit_errors_are_not_proposal_errors(monkeypatch):
    runtime = hinted_runtime()
    ctx = build_repair_context(runtime, "Question?", persist=False)
    with pytest.raises(ValueError, match="PLAN_REPAIR_CONTEXT_NOT_REGISTERED") as unregistered:
        apply_repair(runtime, "NONE", ctx)
    assert type(unregistered.value) is ValueError
    from delaybind_core.plan_repair_r2 import register_repair_context
    register_repair_context(runtime, ctx)
    stale = {**ctx, "state_revision": ctx["state_revision"] - 1}
    with pytest.raises(ValueError, match="STALE_PLAN_REPAIR_CONTEXT") as stale_error:
        apply_repair(runtime, "NONE", stale)
    assert type(stale_error.value) is ValueError

    def broken_commit(*args, **kwargs):
        raise ValueError("INVARIANT_FAILURE")

    monkeypatch.setattr(runtime, "commit", broken_commit)
    with pytest.raises(ValueError, match="INVARIANT_FAILURE") as commit_error:
        apply_repair(runtime, "NONE", ctx)
    assert type(commit_error.value) is ValueError


def test_invalid_plan_and_unauthorized_fact_never_publish_partial_patch():
    runtime = hinted_runtime()
    ctx = build_repair_context(runtime, "Question?")
    before = runtime.export()
    raw = ("REPAIR | Fnot_allowed\n"
           "UPSERT | Q4\nquery: Who knew Alice?\noutput: ?knower\n"
           "depends_on: NONE\nEND UPSERT\nEND REPAIR")
    with pytest.raises(PlanRepairProposalError, match="PLAN_REPAIR_EVIDENCE_REQUIRED"):
        apply_repair(runtime, raw, ctx)
    assert runtime.export() == before


def test_basis_ignores_revision_and_duplicate_hint_but_changes_for_new_fact():
    runtime = hinted_runtime()
    first = repair_basis_key(runtime.state, build_repair_context(runtime, "Question?", persist=False))
    staged = runtime.state.model_copy(deep=True)
    runtime.commit(staged, [("AUDIT_ONLY", {})])
    assert repair_basis_key(runtime.state, build_repair_context(runtime, "Question?", persist=False)) == first
    ctx = build_update_context(runtime, "Question?", ["D0:S0"])
    duplicate, rejected = parse_update("PLAN_HINT | A new alias relation is needed.", ctx)
    assert rejected == []
    runtime.ingest(duplicate, ctx)
    assert repair_basis_key(runtime.state, build_repair_context(runtime, "Question?", persist=False)) == first
    ctx = build_update_context(runtime, "Question?", ["D0:S0"])
    fresh, rejected = parse_update("Q1 | Cindy's teacher is Alice.", ctx)
    assert rejected == []
    runtime.ingest(fresh, ctx)
    assert repair_basis_key(runtime.state, build_repair_context(runtime, "Question?", persist=False)) != first


def test_rejection_write_failure_rolls_back_and_propagates(monkeypatch):
    from delaybind_core.plan_repair_r2 import register_repair_context

    runtime = hinted_runtime()
    ctx = build_repair_context(runtime, "Question?", persist=False)
    register_repair_context(runtime, ctx)
    key = repair_basis_key(runtime.state, ctx)
    before = runtime.export()
    database = list(runtime.store.connection.iterdump())

    def fail_write(*args, **kwargs):
        raise sqlite3.OperationalError("injected rejection write failure")

    monkeypatch.setattr(runtime.store, "_r2_execute", fail_write)
    with pytest.raises(sqlite3.OperationalError, match="injected rejection write failure"):
        record_plan_repair_rejection(runtime, ctx, key, ["PLAN_REPAIR_FORMAT"], ["response-hash"])
    assert runtime.export() == before
    assert list(runtime.store.connection.iterdump()) == database
