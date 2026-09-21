import asyncio
import sqlite3
import pytest

from test_r2_core import (fixture, chain, ingest, context, proposal, review_all, finalize, recall_all, bound_chain,
                          current_uses, evidence_pack, assert_invariants, query_ready, next_job)
from delaybind_core.schema_v52 import QueryPlanV3, digest
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.replay import replay_events
from delaybind_core.plan_repair_r2 import build_repair_context, apply_repair
from delaybind_core.runner import RunnerConfig
from delaybind_core.api import APIConfig, OpenAICompatibleClient
from delaybind_core.schema import ModelCall
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.review_jobs_r2 import ensure_review


def test_r01_every_sql_write_failure_rolls_back_state_graphs_jobs_and_receipt(monkeypatch):
    # Find the actual transaction's write count, then inject once at *each* seam.
    def prepared():
        r = fixture()
        a, _, _ = bound_chain(r)
        new = ingest(r, [("Q1", "D0:S3")])[0]
        ctx = review_all(r, "Q1", {a: "REJECT"})
        p = proposal(ctx, result=dict(state="BOUND", value="Bob", kind="DIRECT", support_fact_ids=[new],
                                     decision_source_refs=["D0:S3"], reason_code="FIXTURE", reason="explicit correction"))
        return r, ctx, p
    probe, ctx, p = prepared()
    calls = []
    original = probe.store._r2_execute
    def count(db, sql, params):
        calls.append(sql)
        return original(db, sql, params)
    monkeypatch.setattr(probe.store, "_r2_execute", count)
    apply_memory(probe, p, ctx)
    assert len(calls) > 20
    for fail_at in range(1, len(calls) + 1):
        r, ctx, p = prepared()
        before = r.export()
        database = list(r.store.connection.iterdump())
        executed = 0
        write = r.store._r2_execute
        def fault(db, sql, params):
            nonlocal executed
            executed += 1
            if executed == fail_at:
                raise sqlite3.OperationalError("injected")
            return write(db, sql, params)
        monkeypatch.setattr(r.store, "_r2_execute", fault)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            apply_memory(r, p, ctx)
        assert r.export() == before
        assert list(r.store.connection.iterdump()) == database
        assert replay_events(r.store.list_runtime_events("r")).export() == before
        r.store.close()


def test_r02_commit_before_publish_crash_recovers_from_disk(monkeypatch):
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = review_all(r, "Q1")
    p = proposal(ctx, result=dict(state="BOUND", value="Alice", support_fact_ids=[a], reason_code="TEST", reason="test"))
    commit = r.store.commit_r2
    before = r.export()
    def die(**kwargs):
        commit(**kwargs)
        raise SystemExit("process exit before publication")
    monkeypatch.setattr(r.store, "commit_r2", die)
    with pytest.raises(SystemExit):
        apply_memory(r, p, ctx)
    assert r.export() == before
    restored = RuntimeR2(run_id="r", plan=r.state.plan, archive=r.archive, store=r.store, config=r.config)
    assert restored.export()["bindings"] == {"?teacher": "Alice"}
    saved = restored.export()
    apply_memory(restored, p, ctx)
    assert restored.export() == saved


def test_r03_recall_outbox_recovery_lease_and_batch_redelivery_are_idempotent():
    r = fixture()
    a, b = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1")])
    finalize(r, "Q1", "Alice", [a])
    job = next_job(r.state)
    first = r.claim(job.job_id)
    lease = first.lease
    restored = RuntimeR2(run_id="r", plan=r.state.plan, archive=r.archive, store=r.store, config=r.config)
    reclaimed = restored.claim(job.job_id)
    assert reclaimed.lease != lease and reclaimed.retry_count == 1
    restored.finish_recall_batch(job.job_id, [b], expected_offset=0)
    before = restored.export()
    restored.finish_recall_batch(job.job_id, [b], expected_offset=0)
    assert before == restored.export()
    with pytest.raises(ValueError, match="CONTEXT_CONSUMED"):
        restored.finish_recall_batch(job.job_id, [], expected_offset=0)


def test_r10_model_log_cannot_commit_or_rollback_runtime_transaction():
    store = SQLiteEventStore()
    with store.transaction() as db:
        db.execute("INSERT INTO runtime_state VALUES ('uncommitted', 1, 'test', '{}')")
        with pytest.raises(RuntimeError, match="NESTED_TRANSACTION"):
            store.append_model_call(ModelCall(call_id="c", run_id="r", interface="ANSWER", request_hash="h", prompt_hash="p", model="fixture"))
        assert db.in_transaction
        assert db.execute("SELECT COUNT(*) FROM runtime_state").fetchone()[0] == 1
    assert store.connection.execute("SELECT COUNT(*) FROM runtime_state").fetchone()[0] == 1


def test_r17_local_audit_fields_never_enter_http_and_cache_modes_are_isolated(monkeypatch):
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(APIConfig(base_url="http://unused", api_key="fixture", model="fixture", max_retries=0), store=store)
    sent = []
    def call(payload):
        sent.append(payload)
        assert not set(payload) & {"memory_mode", "review_phase", "context_id", "review_id", "raw_bundle_hash", "protocol_version"}
        return {"choices": [{"message": {"content": "fixture"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}, 1.0
    monkeypatch.setattr(client, "_call_sync", call)
    base = dict(protocol_version="v5.2-r2", memory_mode="BIND", review_phase="FINAL", context_id="C1", review_id="R1", raw_bundle_hash="hash")
    async def invoke(metadata):
        return await client.complete(run_id="r", interface="MEMORY", messages=[{"role": "user", "content": "same prompt"}],
                                     agent_role="HIGH", local_metadata=metadata)
    asyncio.run(invoke(base))
    asyncio.run(invoke(base))
    assert len(sent) == 1
    for change in ({"memory_mode": "REBIND"}, {"context_id": "C2"}, {"raw_bundle_hash": "other"}, {"review_phase": "REVIEW"}):
        asyncio.run(invoke({**base, **change}))
    assert len(sent) == 5
    logs = store.list_model_calls("r")
    assert logs[1].cache_hit and logs[0].memory_mode == "BIND"


def test_r12_plan_repair_version_invalidates_union_and_routes_ready_query():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", plan_repair_mode="on_hint"))
    a, b, c = bound_chain(r)
    ctx = build_repair_context(r, "Question?")
    raw = f"REPAIR | {a}\nUPSERT | Q2\nquery: Who is ?teacher's father?\nEND UPSERT\nROUTE | Q2 | {a}\nEND REPAIR"
    apply_repair(r, raw, ctx)
    assert r.state.executions["Q2"].version == 2
    assert r.state.binding_store["B1"].valid
    assert not r.state.binding_store["B2"].valid and not r.state.binding_store["B3"].valid
    assert r.state.executions["Q3"].status == "DORMANT"
    assert next_job(r.state).kind == "RECALL"
    assert a in r.state.route_index["Q2"]
    assert b in r.state.route_index["Q2"] and c in r.state.route_index["Q3"]
    before = r.export()
    apply_repair(r, raw, ctx)
    assert before == r.export()


def test_r12_plan_repair_can_add_query_but_not_create_cycles():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", plan_repair_mode="on_hint"))
    a = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = build_repair_context(r, "?")
    raw = f"REPAIR | {a}\nUPSERT | Q4\nquery: Other?\noutput: ?other\ndepends_on: NONE\nEND UPSERT\nROUTE | Q4 | {a}\nEND REPAIR"
    apply_repair(r, raw, ctx)
    assert "Q4" in r.state.executions
    assert any(j.target_query == "Q4" and j.kind == "RECALL" for j in r.state.jobs.values())
    ctx = build_repair_context(r, "?")
    before = r.export()
    with pytest.raises(ValueError):
        apply_repair(r, f"REPAIR | {a}\nUPSERT | Q1\nquery: Who knows ?city?\ndepends_on: Q3\nEND UPSERT\nEND REPAIR", ctx)
    assert r.export() == before


def test_plan_repair_cannot_add_final_comparison_node():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", plan_repair_mode="on_hint"))
    a = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = build_repair_context(r, "Are Cindy's teacher and the teacher's mother the same person?")
    raw = (f"REPAIR | {a}\nUPSERT | Q4\n"
           "query: Are ?teacher and ?mother the same person?\n"
           "output: ?same\n"
           "depends_on: Q1,Q2\n"
           "END UPSERT\nEND REPAIR")
    before = r.export()
    with pytest.raises(ValueError, match="PLAN_REPAIR_FINAL_COMPUTATION_FORBIDDEN"):
        apply_repair(r, raw, ctx)
    assert r.export() == before


def test_d09_d10_multi_parent_diamond_waits_and_invalidates_each_descendant_once():
    plan = QueryPlanV3(plan_id="diamond", queries=[
        dict(id="Q1", template="Root?", output="?root"),
        dict(id="Q2", template="Left ?root?", output="?left", inputs={"?root": "Q1"}),
        dict(id="Q3", template="Right ?root?", output="?right", inputs={"?root": "Q1"}),
        dict(id="Q4", template="Join ?left and ?right?", output="?join", inputs={"?left": "Q2", "?right": "Q3"})])
    r = fixture(plan=plan)
    a, b, c, d = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1"), ("Q3", "D0:S2"), ("Q4", "D0:S4")])
    finalize(r, "Q1", "root", [a])
    recall_all(r, "Q2")
    finalize(r, "Q2", "left", [b])
    assert not query_ready(r.state, "Q4")
    recall_all(r, "Q3")
    finalize(r, "Q3", "right", [c])
    recall_all(r, "Q4")
    finalize(r, "Q4", "join", [d])
    new = ingest(r, [("Q1", "D0:S3")])[0]
    finalize(r, "Q1", "newroot", [new])
    retired = [e.payload["binding_id"] for e in r.store.list_runtime_events("r") if e.event_type == "BINDING_RETIRED"]
    assert retired.count("B4") == 1
    assert not query_ready(r.state, "Q4")
    assert not [j for j in r.state.jobs.values() if j.target_query == "Q4" and j.status == "PENDING"]


@pytest.mark.parametrize("value", ["Alice", "Bob"])
def test_d11_d15_independent_review_barrier_survives_other_branch_reaffirm(value):
    plan = QueryPlanV3(plan_id="independent", queries=[dict(id=f"Q{i}", template=f"Need {i}?", output=f"?v{i}") for i in (1, 2)])
    r = fixture(plan=plan)
    a, b = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1")])
    finalize(r, "Q1", "Alice", [a])
    finalize(r, "Q2", "Mary", [b])
    x, y = ingest(r, [("Q1", "D0:S3"), ("Q2", "D0:S4")])
    blocked = list(r.state.executions["Q2"].blocking_review_ids)
    old_binding = r.state.binding_store["B2"].model_copy(deep=True)
    finalize(r, "Q1", value, [a], verdicts={x: "REJECT"})
    assert r.state.executions["Q2"].blocking_review_ids == blocked
    assert r.state.binding_store["B2"] == old_binding
    assert r.export()["bindings"] == {"?v1": value}


def test_d16_parent_counterevidence_and_child_new_fact_are_registered_before_barrier():
    r = fixture()
    a, b, c = bound_chain(r)
    new_parent, new_child = ingest(r, [("Q2", "D0:S4"), ("Q1", "D0:S3")])[::-1]
    old_signature = r.state.executions["Q2"].input_signature
    assert next_job(r.state).target_query == "Q1"
    assert new_child in r.state.route_index["Q2"]
    finalize(r, "Q1", "Bob", [new_parent], verdicts={a: "REJECT"})
    recall = next_job(r.state)
    assert recall.target_query == "Q2" and {b, new_child} <= set(recall.payload["candidates"])
    assert all(u.status == "INVALIDATED" for u in r.state.uses.values() if u.query_id == "Q2" and u.fact_id == new_child and u.input_signature == old_signature)
    assert next(u for u in current_uses(r.state, "Q2") if u.fact_id == new_child).status == "CANDIDATE"


def test_d17_correction_stages_local_fact_then_notifies_other_query_without_silent_replace():
    plan = QueryPlanV3(plan_id="p", queries=[dict(id=f"Q{i}", template=f"Need {i}?", output=f"?v{i}") for i in (1, 2)])
    r = fixture(plan=plan)
    old, _ = ingest(r, [("Q1", "D0:S0", "Wrong extraction."), ("Q2", "D0:S0", "Wrong extraction.")])
    finalize(r, "Q2", "Wrong", [old])
    review_all(r, "Q1", {old: dict(verdict="ACCEPT", correction=dict(local_id="N1", text="Alice teaches Cindy.", source_refs=["D0:S0"]))})
    corrected = next(f.fact_id for f in r.state.facts.values() if f.supersedes_fact_id)
    assert r.state.facts[old].text == "Wrong extraction."
    finalize(r, "Q1", "Alice", [corrected])
    assert r.state.binding_store["B1"].direct_fact_ids == [old]
    assert r.state.binding_store["B1"].valid
    assert r.state.executions["Q2"].blocking_review_ids
    assert {old, corrected} <= {r.state.uses[k].fact_id for k in r.state.reviews[r.state.executions["Q2"].blocking_review_ids[0]].required_use_ids}
    assert len(r.state.correction_notifications) == 1


def test_d08_rejected_old_input_can_be_recalled_under_new_parent_signature():
    r = fixture()
    a, b = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1")])
    finalize(r, "Q1", "Alice", [a])
    recall_all(r, "Q2")
    finalize(r, "Q2", None, [], verdicts={b: "REJECT"})
    new = ingest(r, [("Q1", "D0:S3")])[0]
    finalize(r, "Q1", "Bob", [new])
    job = next_job(r.state)
    assert b in job.payload["candidates"]
    recall_all(r, "Q2")
    assert b in context(r, "Q2").required_reviews


def test_r11_truncated_transaction_does_not_publish_and_tampering_is_rejected():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    finalize(r, "Q1", "Alice", [a])
    events = r.store.list_runtime_events("r")
    assert replay_events(events[:-1]).state_revision == r.state.state_revision - 1
    changed = [e.model_copy(deep=True) for e in events]
    changed[-2].payload["tampered"] = True
    with pytest.raises(ValueError, match="INCOMPLETE_TRANSACTION"):
        replay_events(changed)


def test_d09_other_parent_pending_then_reaffirm_wakes_join_exactly_once():
    plan = QueryPlanV3(plan_id="p", queries=[dict(id="Q1", template="First?", output="?a"),
        dict(id="Q2", template="Second?", output="?b"),
        dict(id="Q3", template="Join ?a ?b?", output="?c", inputs={"?a": "Q1", "?b": "Q2"})])
    r = fixture(plan=plan)
    a, b, c = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1"), ("Q3", "D0:S2")])
    finalize(r, "Q1", "a", [a])
    finalize(r, "Q2", "b", [b])
    recall_all(r, "Q3")
    finalize(r, "Q3", "c", [c])
    x, y = ingest(r, [("Q1", "D0:S3"), ("Q2", "D0:S4")])
    finalize(r, "Q1", "new a", [x])
    assert not query_ready(r.state, "Q3")
    assert not [j for j in r.state.jobs.values() if j.target_query == "Q3" and j.status == "PENDING"]
    finalize(r, "Q2", "b", [b], verdicts={y: "REJECT"})
    assert query_ready(r.state, "Q3")
    assert len([j for j in r.state.jobs.values() if j.target_query == "Q3" and j.status == "PENDING"]) == 1


def test_immutable_records_cannot_be_rewritten_by_a_staged_delta():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    finalize(r, "Q1", "Alice", [a])
    before = r.export()
    s = r.state.model_copy(deep=True)
    s.facts[a] = s.facts[a].model_copy(update={"text": "tampered"})
    with pytest.raises(ValueError, match="IMMUTABLE_RECORD_CHANGED"):
        r.commit(s, [])
    s = r.state.model_copy(deep=True)
    s.binding_store["B1"].value = "tampered"
    with pytest.raises(ValueError, match="IMMUTABLE_BINDING_CHANGED"):
        r.commit(s, [])
    assert before == r.export()
