"""Empty work is a Runtime decision; explicit pure upstream hops are allowed."""

import pytest

from delaybind_core.context_r2 import build_memory_context, persist_memory_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.plan_goal_r2 import parse_v51_member_plan
from delaybind_core.plan_repair_r2 import parse_repair
from delaybind_core.protocol_r2 import parse_memory
from delaybind_core.review_jobs_r2 import next_job
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema_r2 import EvidencePlanR2
from test_r2_core import fixture
from test_r2_strict_admission import update, bind_root


def test_ordinary_dependent_query_without_candidates_is_skipped():
    r = fixture(plan=EvidencePlanR2(plan_id="empty", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who is ?teacher's mother?", output="?mother", inputs={"?teacher": "Q1"}),
    ]), config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    bind_root(r)
    assert next_job(r.state) is None
    assert r.state.executions["Q2"].status == "ACTIVE"
    assert [e for e in r.store.list_runtime_events(r.run_id) if e.event_type == "MEMORY_SKIPPED"
            and e.payload["query_id"] == "Q2"]


def test_explicit_intermediate_upstream_only_hop_can_bind_without_direct_fact():
    plan = EvidencePlanR2(plan_id="upstream", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who is ?teacher?", output="?same", inputs={"?teacher": "Q1"},
             allow_upstream_only=True),
        dict(id="Q3", template="Where was ?same born?", output="?city", inputs={"?same": "Q2"}),
    ])
    r = fixture(plan=plan, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    bind_root(r)
    job = next_job(r.state)
    assert job.kind == "MEMORY" and job.target_query == "Q2"
    ctx = build_memory_context(r, job.review_id)
    assert ctx.allowed_fact_ids == [] and ctx.upstream_only_allowed
    persist_memory_context(r, ctx)
    apply_memory(r, parse_memory("BOUND | Alice | NONE", ctx), ctx)
    assert r.state.executions["Q2"].current_binding_id


def test_invalid_upstream_only_declarations_and_values_are_rejected():
    with pytest.raises(ValueError, match="UPSTREAM_ONLY_REQUIRES_INTERMEDIATE_DEPENDENCY"):
        EvidencePlanR2(plan_id="invalid", queries=[
            dict(id="Q1", template="Who teaches Cindy?", output="?teacher", allow_upstream_only=True)])
    with pytest.raises(ValueError, match="UPSTREAM_ONLY_REQUIRES_INTERMEDIATE_DEPENDENCY"):
        EvidencePlanR2(plan_id="invalid", queries=[
            dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
            dict(id="Q2", template="Who is ?teacher?", output="?same", inputs={"?teacher": "Q1"},
                 allow_upstream_only=True)])
    raw = ("Q1\nquery: Who teaches Cindy?\noutput: ?teacher\ndepends_on: NONE\n"
           "Q2\nquery: Who is ?teacher?\noutput: ?same\ndepends_on: Q1\n"
           "allow_upstream_only: maybe")
    with pytest.raises(ValueError, match="INVALID_OR_DUPLICATE_UPSTREAM_ONLY_FIELD"):
        parse_v51_member_plan(raw, "Who is Cindy's teacher?")


def test_initial_plan_and_field_only_repair_preserve_explicit_permission():
    raw = ("Q1\nquery: Who teaches Cindy?\noutput: ?teacher\ndepends_on: NONE\n"
           "Q2\nquery: Who is ?teacher?\noutput: ?same\ndepends_on: Q1\n"
           "allow_upstream_only: true\n"
           "Q3\nquery: Where was ?same born?\noutput: ?city\ndepends_on: Q2")
    plan = parse_v51_member_plan(raw, "Where was Cindy's teacher born?")
    assert plan.queries[1].allow_upstream_only
    ops, _ = parse_repair("REPAIR | F1\nUPSERT | Q2\nallow_upstream_only: false\n"
                          "END UPSERT\nEND REPAIR", plan)
    assert ops[0]["patch"]["allow_upstream_only"] is False
