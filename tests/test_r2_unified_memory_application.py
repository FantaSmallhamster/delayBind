"""Unified evidence results preserve atomic proofs and frozen permissions."""

import pytest

from delaybind_core.context_r2 import persist_memory_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.navigation_r2 import assert_invariants, binding_effective, current_uses
from delaybind_core.protocol_r2 import parse_memory
from delaybind_core.replay import replay_events
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import EvidencePlanR2
from test_r2_core import context, fixture
from test_r2_member_graph import ingest


def runtime(plan=None):
    plan = plan or EvidencePlanR2(plan_id="unified-application", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who is ?teacher's mother?", output="?mother", inputs={"?teacher": "Q1"}),
        dict(id="Q3", template="Where was ?mother born?", output="?city", inputs={"?mother": "Q2"}),
    ])
    return fixture(plan=plan, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                                                 memory_interface="unified_evidence_v1"))


def response(ctx, *answers):
    aliases = {fid: alias for alias, fid in ctx.fact_aliases.items()}
    raw = "\n".join(f"{ctx.query_id} | {','.join(aliases[fid] for fid in facts) or 'NONE'} | {value}"
                    for value, facts in answers) if answers else f"{ctx.query_id} | NONE | UNKNOWN"
    return parse_memory(raw, ctx)


def decide(r, qid, *answers):
    ctx = context(r, qid)
    parsed = response(ctx, *answers)
    receipt = apply_memory(r, parsed, ctx)
    assert_invariants(r.state)
    return ctx, parsed, receipt


def current(r, qid):
    return r.state.binding_store[r.state.executions[qid].current_binding_id]


def events(r, name):
    return [event.payload for event in r.store.list_runtime_events(r.run_id) if event.event_type == name]


def seed_chain(r):
    teacher, mother = ingest(r, ("Q1", "Cindy's teacher is Alice."), ("Q2", "Alice's mother is Mary."))
    decide(r, "Q1", ("Alice", [teacher]))
    decide(r, "Q2", ("Mary", [mother]))
    return teacher, mother


def test_unknown_preserves_candidates_without_creating_a_binding():
    r = runtime()
    fid, = ingest(r, ("Q1", "Cindy's father is Elias."))
    ctx, _, receipt = decide(r, "Q1")
    assert not receipt["changed"] and receipt["review_complete"]
    assert r.state.executions["Q1"].current_binding_id is None
    assert r.state.executions["Q1"].retry_gate
    assert r.state.route_index["Q1"] == [fid]
    assert all(use.status == "CANDIDATE" for use in current_uses(r.state, "Q1"))
    assert r.state.diagnostics[-1]["state"] == "UNSUPPORTED_RESULT"
    assert not events(r, "BINDING_REAFFIRMED")
    assert events(r, "MEMORY_RESULT_EVALUATED")[-1]["unknown_result"]
    published = events(r, "UNIFIED_MEMORY_RESULT_APPLIED")[-1]
    assert published["unknown_result"] and not published["unchanged"] and not published["binding_changed"]
    assert published["supported_member_count"] == 0
    assert r.state.reviews[ctx.review_id].authorized_use_ids


def test_unknown_during_rebind_keeps_prior_binding_and_dependent_chain():
    r = runtime()
    seed_chain(r)
    old, child = current(r, "Q1").binding_id, current(r, "Q2").binding_id
    new, = ingest(r, ("Q1", "Cindy's teacher has an uncertain identity."))
    _, _, receipt = decide(r, "Q1")
    assert not receipt["changed"]
    assert current(r, "Q1").binding_id == old and current(r, "Q2").binding_id == child
    assert binding_effective(r.state, child)
    assert next(use for use in current_uses(r.state, "Q1") if use.fact_id == new).status == "CANDIDATE"
    assert not events(r, "BINDING_REAFFIRMED")
    result = events(r, "UNIFIED_MEMORY_RESULT_APPLIED")[-1]
    assert result["unknown_result"] and result["snapshot_unchanged"]
    assert not result["unchanged"] and result["preserved_binding_id"] == old


def test_identical_value_and_proof_reaffirms_without_recomputing_descendants():
    r = runtime()
    teacher, _ = seed_chain(r)
    old, child = current(r, "Q1").binding_id, current(r, "Q2").binding_id
    count = len(r.state.binding_store)
    noise, = ingest(r, ("Q1", "Cindy's teacher enjoys hiking."))
    _, _, receipt = decide(r, "Q1", ("Alice", [teacher]))
    assert not receipt["changed"] and len(r.state.binding_store) == count
    assert current(r, "Q1").binding_id == old and current(r, "Q2").binding_id == child
    assert binding_effective(r.state, child)
    assert next(use for use in current_uses(r.state, "Q1") if use.fact_id == noise).status == "CANDIDATE"
    result = events(r, "UNIFIED_MEMORY_RESULT_APPLIED")[-1]
    assert result["unchanged"] and not result["unknown_result"] and not result["binding_changed"]
    assert events(r, "BINDING_REAFFIRMED")[-1]["binding_id"] == old


def test_same_value_with_new_proof_retires_dependent_proofs_but_keeps_facts():
    r = runtime()
    teacher, mother = seed_chain(r)
    old, child = current(r, "Q1").binding_id, current(r, "Q2").binding_id
    replacement, = ingest(r, ("Q1", "Alice teaches Cindy."))
    _, _, receipt = decide(r, "Q1", ("Alice", [replacement]))
    assert receipt["changed"] and set(receipt["affected_query_ids"]) == {"Q1", "Q2", "Q3"}
    assert not r.state.binding_store[old].valid and not r.state.binding_store[child].valid
    assert current(r, "Q1").direct_fact_ids == [replacement]
    assert {teacher, replacement} <= set(r.state.route_index["Q1"])
    assert mother in r.state.route_index["Q2"] and context(r, "Q2").allowed_fact_ids == [mother]
    assert not any(job.kind == "RECALL" for job in r.state.jobs.values())
    result = events(r, "UNIFIED_MEMORY_RESULT_APPLIED")[-1]
    assert result["binding_changed"] and not result["unchanged"]


def seed_fanout(r):
    alice, bob, mary, june = ingest(r, ("Q1", "Alice teaches Cindy."), ("Q1", "Bob teaches Cindy."),
                                  ("Q2", "Alice's mother is Mary."), ("Q2", "Bob's mother is June."))
    decide(r, "Q1", ("Alice", [alice]), ("Bob", [bob]))
    return {"Alice": ("Mary", [mary]), "Bob": ("June", [june])}


def test_branch_staging_is_atomic_and_survives_resume_and_duplicate_submission():
    r = runtime()
    answers = seed_fanout(r)
    name = context(r, "Q2").branch.bound_inputs["?teacher"]
    ctx, parsed, receipt = decide(r, "Q2", answers[name])
    assert receipt["branch_staged"] and not receipt["review_complete"]
    assert r.state.executions["Q2"].current_binding_id is None
    assert all(use.status != "ACCEPTED" for use in current_uses(r.state, "Q2"))
    assert apply_memory(r, parsed, ctx) == receipt
    restored = RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive, store=r.store, config=r.config)
    other = context(restored, "Q2").branch.bound_inputs["?teacher"]
    assert other != name
    decide(restored, "Q2", answers[other])
    binding = current(restored, "Q2")
    assert {member.value for member in binding.members} == {"Mary", "June"}
    parent_names = {member.member_id: member.value for member in current(restored, "Q1").members}
    for member in binding.members:
        parent = parent_names[member.parent_member_ids[0]]
        assert (member.value, member.direct_fact_ids) == answers[parent]
    assert replay_events(r.store.list_runtime_events(r.run_id)).export() == restored.export()


def test_mixed_unknown_and_unchanged_branches_preserve_snapshot_without_reaffirming_unknown():
    r = runtime()
    answers = seed_fanout(r)
    for _ in range(2):
        name = context(r, "Q2").branch.bound_inputs["?teacher"]
        decide(r, "Q2", answers[name])
    old = current(r, "Q2").binding_id
    ingest(r, ("Q2", "Teachers have family backgrounds."))
    decide(r, "Q2")
    name = context(r, "Q2").branch.bound_inputs["?teacher"]
    _, _, receipt = decide(r, "Q2", answers[name])
    assert not receipt["changed"] and current(r, "Q2").binding_id == old
    result = events(r, "UNIFIED_MEMORY_RESULT_APPLIED")[-1]
    assert result["snapshot_unchanged"] and result["unknown_branch_count"] == 1
    assert not result["unchanged"] and not result["unknown_result"]
    assert result["supported_member_count"] == 1
    assert not events(r, "BINDING_REAFFIRMED")


@pytest.mark.parametrize("mutation, expected", [
    (lambda ctx: ctx.model_copy(update={"authorized_use_ids": []}), "UNIFIED_EVIDENCE_PERMISSION_MISMATCH"),
    (lambda ctx: ctx.model_copy(update={"allowed_fact_ids": []}), "UNIFIED_EVIDENCE_PERMISSION_MISMATCH"),
    (lambda ctx: ctx.model_copy(update={"fact_aliases": {"F1": "foreign-fact"}}), "UNIFIED_EVIDENCE_PERMISSION_MISMATCH"),
    (lambda ctx: ctx.model_copy(update={"evidence_digest": "foreign-evidence"}), "UNIFIED_EVIDENCE_PERMISSION_MISMATCH"),
    (lambda ctx: ctx.model_copy(update={"admission_policy": "legacy"}), "UNIFIED_EVIDENCE_PERMISSION_MISMATCH"),
    (lambda ctx: ctx.model_copy(update={"memory_interface": "legacy_bind_rebind_v1"}), "MEMORY_INTERFACE_MISMATCH"),
    (lambda ctx: ctx.model_copy(update={"upstream_only_allowed": True}), "UPSTREAM_ONLY_PERMISSION_MISMATCH"),
])
def test_registered_but_unauthorized_context_cannot_relax_frozen_permissions(mutation, expected):
    r = runtime()
    fid, = ingest(r, ("Q1", "Alice teaches Cindy."))
    ctx = context(r, "Q1")
    parsed = response(ctx, ("Alice", [fid]))
    tampered = mutation(ctx).model_copy(update={"context_id": ctx.context_id + "-tampered"})
    persist_memory_context(r, tampered)
    parsed = parsed.model_copy(update={"context_id": tampered.context_id})
    before = r.export()
    with pytest.raises(ValueError, match=expected):
        apply_memory(r, parsed, tampered)
    assert r.export() == before


def test_changed_evidence_makes_inflight_result_stale_without_partial_binding():
    r = runtime()
    fid, = ingest(r, ("Q1", "Alice teaches Cindy."))
    ctx = context(r, "Q1")
    parsed = response(ctx, ("Alice", [fid]))
    ingest(r, ("Q1", "Bob also teaches Cindy."))
    before = r.export()
    with pytest.raises(ValueError, match="STALE_MEMORY_CONTEXT"):
        apply_memory(r, parsed, ctx)
    assert r.export() == before and not r.state.binding_store


def test_changed_upstream_path_rejects_previous_child_result():
    r = runtime()
    teacher, mother = ingest(r, ("Q1", "Alice teaches Cindy."), ("Q2", "Alice's mother is Mary."))
    decide(r, "Q1", ("Alice", [teacher]))
    ctx = context(r, "Q2")
    parsed = response(ctx, ("Mary", [mother]))
    replacement, = ingest(r, ("Q1", "Bob teaches Cindy."))
    decide(r, "Q1", ("Bob", [replacement]))
    before = r.export()
    with pytest.raises(ValueError, match="STALE_MEMORY_CONTEXT"):
        apply_memory(r, parsed, ctx)
    assert r.export() == before and r.state.executions["Q2"].current_binding_id is None


def test_forged_fact_body_or_branch_cannot_be_applied_even_if_registered():
    r = runtime()
    fid, = ingest(r, ("Q1", "Alice teaches Cindy."))
    ctx = context(r, "Q1")
    parsed = response(ctx, ("Alice", [fid]))
    body = ctx.model_copy(deep=True, update={"context_id": ctx.context_id + "-body"})
    body.working_memory.navigation["facts"][0]["text"] = "Bob teaches Cindy."
    persist_memory_context(r, body)
    before = r.export()
    with pytest.raises(ValueError, match="UNIFIED_EVIDENCE_BODY_MISMATCH"):
        apply_memory(r, parsed.model_copy(update={"context_id": body.context_id}), body)
    assert r.export() == before
    branch = ctx.model_copy(deep=True, update={"context_id": ctx.context_id + "-branch"})
    branch.branch.lineage = {"Q9": "foreign-member"}
    persist_memory_context(r, branch)
    with pytest.raises(ValueError, match="STALE_MEMBER_BRANCH"):
        apply_memory(r, parsed.model_copy(update={"context_id": branch.context_id}), branch)
    assert r.export() == before


def test_upstream_only_proof_requires_plan_qualification_even_during_replay():
    from delaybind_core.member_graph_r2 import assert_member_invariants

    plan = EvidencePlanR2(plan_id="upstream-inference", queries=[
        dict(id="Q1", template="What is the starting number?", output="?number"),
        dict(id="Q2", template="What is twice ?number?", output="?doubled", inputs={"?number": "Q1"},
             allow_upstream_only=True),
        dict(id="Q3", template="What label identifies ?doubled?", output="?label", inputs={"?doubled": "Q2"}),
    ])
    r = runtime(plan)
    number, = ingest(r, ("Q1", "The starting number is 10."))
    decide(r, "Q1", (10, [number]))
    ctx, _, _ = decide(r, "Q2", (20, []))
    assert ctx.upstream_only_allowed and not ctx.allowed_fact_ids
    assert current(r, "Q2").kind == "INFERRED"
    # An upstream review barrier temporarily hides the proof but does not make
    # its structural inference authorization invalid.
    ingest(r, ("Q1", "The starting number appears on the worksheet."))
    assert not binding_effective(r.state, current(r, "Q2").binding_id)
    assert_invariants(r.state)
    corrupt = r.state.model_copy(deep=True)
    corrupt.plan.queries[1].allow_upstream_only = False
    with pytest.raises(ValueError, match="UPSTREAM_ONLY_NOT_AUTHORIZED"):
        assert_member_invariants(corrupt)


def test_internal_adapter_cannot_apply_a_view_containing_extra_unapproved_facts():
    r = runtime()
    fid, = ingest(r, ("Q1", "Alice teaches Cindy."))
    ctx = context(r, "Q1")
    parsed = response(ctx, ("Alice", [fid]))
    tampered = ctx.model_copy(deep=True, update={"context_id": ctx.context_id + "-extra"})
    tampered.working_memory.navigation["facts"].append({"fact_id": "extra", "text": "Injected fact."})
    persist_memory_context(r, tampered)
    before = r.export()
    with pytest.raises(ValueError, match="UNIFIED_EVIDENCE_BODY_MISMATCH"):
        apply_memory(r, parsed.model_copy(update={"context_id": tampered.context_id}), tampered)
    assert r.export() == before


def test_legacy_registered_memory_snapshot_without_new_fields_remains_usable():
    from delaybind_core.context_r2 import build_memory_context

    plan = EvidencePlanR2(plan_id="legacy-snapshot", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher")])
    r = fixture(plan=plan, config=RunnerConfig(sentence_splitting=False))
    fid, = ingest(r, ("Q1", "Alice teaches Cindy."))
    session = next(iter(r.state.reviews.values()))
    ctx = build_memory_context(r, session.review_id)
    frozen = ctx.model_dump(mode="json")
    for key in ("memory_interface", "authorized_use_ids", "evidence_origins", "evidence_digest", "fact_aliases"):
        frozen.pop(key)
    r.store.save_context_manifest(r.run_id, ctx.context_id, {"interface": "MEMORY_SNAPSHOT", **frozen})
    receipt = apply_memory(r, parse_memory("BOUND | Alice | " + fid, ctx), ctx)
    assert receipt["changed"] and current(r, "Q1").value == "Alice"
