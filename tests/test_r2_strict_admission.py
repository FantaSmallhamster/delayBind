"""Authorization is per query instance and only finalized RECALL choices enter MEMORY."""

import pytest

from delaybind_core.context_r2 import build_update_context, build_memory_context, persist_memory_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.navigation_r2 import current_uses
from delaybind_core.protocol_r2 import parse_update, parse_memory
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema_r2 import EvidencePlanR2, fact_id
from delaybind_core.review_jobs_r2 import next_job
from delaybind_core.runtime_r2 import RuntimeR2
from test_r2_core import fixture


PLAN = EvidencePlanR2(plan_id="strict", queries=[
    dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
    dict(id="Q2", template="Who is ?teacher's mother?", output="?mother", inputs={"?teacher": "Q1"}),
])


def runtime():
    return fixture(plan=PLAN, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))


def update(r, *rows):
    ctx = build_update_context(r, "Who is Cindy's teacher's mother?", ["D0:S0"])
    response, rejected = parse_update("\n".join(f"{qid} | {text}" for qid, text in rows), ctx)
    assert not rejected
    r.ingest(response, ctx)
    return [fact_id(text, []) for _, text in rows]


def bind_root(r):
    fid = update(r, ("Q1", "Cindy's teacher is Alice."))[0]
    job = next_job(r.state)
    assert job.kind == "MEMORY"
    ctx = build_memory_context(r, job.review_id)
    persist_memory_context(r, ctx)
    apply_memory(r, parse_memory("BOUND | Alice | F1", ctx), ctx)
    return fid


def test_recall_subset_is_exact_direct_permission_and_guess_is_rejected():
    r = runtime()
    f1, f2, f3 = update(r, ("Q2", "Alice's mother is Mary."),
                        ("Q2", "Alice's mother is June."),
                        ("Q2", "Bob's mother is Sarah."))
    assert not [u for u in current_uses(r.state, "Q2") if u.admission_token]
    bind_root(r)
    job = next_job(r.state)
    assert job.kind == "RECALL"
    r.finish_recall_batch(job.job_id, [f2], batch_size=3)
    job = next_job(r.state)
    assert job.kind == "MEMORY"
    ctx = build_memory_context(r, job.review_id)
    assert ctx.allowed_fact_ids == [f2]
    assert {n["fact_id"] for n in ctx.working_memory.navigation["facts"]} >= {f2}
    assert all(fid not in ctx.allowed_fact_ids for fid in (f1, f3))
    with pytest.raises(ValueError, match="UNKNOWN_FACT_ALIAS"):
        parse_memory(f"BOUND | June | {f1}", ctx)
    persist_memory_context(r, ctx)
    apply_memory(r, parse_memory("NOOP", ctx), ctx)
    assert r.state.uses[next(u.use_id for u in current_uses(r.state, "Q2") if u.fact_id == f2)].consumed_admission_token
    assert all(u.review_context_id is None for u in current_uses(r.state, "Q2") if u.fact_id in {f1, f3})


def test_empty_recall_skips_memory_and_later_direct_update_reopens_candidate():
    r = runtime()
    old = update(r, ("Q2", "Alice's mother is Mary."))[0]
    bind_root(r)
    recall = next_job(r.state)
    assert recall.kind == "RECALL"
    r.finish_recall_batch(recall.job_id, [], batch_size=1)
    assert next_job(r.state) is None
    assert r.state.executions["Q2"].status == "ACTIVE"
    assert any(e.event_type == "MEMORY_SKIPPED" for e in r.store.list_runtime_events(r.run_id))
    assert r.state.uses[next(u.use_id for u in current_uses(r.state, "Q2") if u.fact_id == old)].admission_token is None
    update(r, ("Q2", "Alice's mother is Mary."))
    job = next_job(r.state)
    assert job.kind == "MEMORY"
    ctx = build_memory_context(r, job.review_id)
    assert ctx.allowed_fact_ids == [old]


def test_partial_recall_never_authorizes_early_memory():
    r = runtime()
    first, second = update(r, ("Q2", "Alice's mother is Mary."),
                           ("Q2", "Alice's mother is June."))
    bind_root(r)
    recall = next_job(r.state)
    r.finish_recall_batch(recall.job_id, [first], batch_size=1)
    assert next_job(r.state).kind == "RECALL"
    assert not [review for review in r.state.reviews.values() if review.query_id == "Q2"]
    r.finish_recall_batch(recall.job_id, [], batch_size=1)
    ctx = build_memory_context(r, next_job(r.state).review_id)
    assert ctx.allowed_fact_ids == [first]
    assert second not in ctx.allowed_fact_ids


def test_no_callback_never_promotes_historical_candidate():
    r = fixture(plan=PLAN, config=RunnerConfig(protocol_version="v5.2-r2",
                sentence_splitting=False, enable_defer_callback=False))
    old = update(r, ("Q2", "Alice's mother is Mary."))[0]
    bind_root(r)
    assert next_job(r.state) is None
    assert not [u for u in current_uses(r.state, "Q2") if u.fact_id == old and u.admission_token]
    update(r, ("Q2", "Alice's mother is Mary."))
    assert next_job(r.state).kind == "MEMORY"


def test_old_policy_state_cannot_resume_as_strict():
    r = runtime()
    staged = r.state.model_copy(deep=True)
    staged.admission_policy = "legacy"
    r.commit(staged, [("TEST_LEGACY_POLICY", {})])
    with pytest.raises(ValueError, match="RESUME_ADMISSION_POLICY_CHANGED"):
        RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive,
                  store=r.store, config=r.config)
