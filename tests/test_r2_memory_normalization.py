"""A standalone NONE is a non-destructive, auditable fact-only NOOP alias."""

import pytest

from delaybind_core.context_r2 import build_update_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.protocol_r2 import parse_memory, parse_update
from delaybind_core.replay import replay_events
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import MemoryResponseR2, member_plan
from delaybind_core.schema_v52 import digest
from test_r2_core import chain, context, fixture, ingest, review_all


def fact_runtime(member_bindings):
    r = fixture(plan=member_plan(chain()) if member_bindings else chain(),
                config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    add_fact(r, "Cindy's teacher is Alice.")
    return r


def add_fact(r, text):
    update_ctx = build_update_context(r, "Who teaches Cindy?", ["D0:S0"])
    response, errors = parse_update(f"Q1 | {text}", update_ctx)
    assert not errors
    r.ingest(response, update_ctx)


def retained_state(r):
    return {
        "facts": {key: value.model_dump() for key, value in r.state.facts.items()},
        "uses": {key: value.model_dump() for key, value in r.state.uses.items()},
        "routes": {key: list(value) for key, value in r.state.route_index.items()},
        "bindings": {key: value.model_dump() for key, value in r.state.binding_store.items()},
    }


@pytest.mark.parametrize("member_bindings", [False, True])
@pytest.mark.parametrize("has_binding", [False, True])
def test_none_preserves_facts_candidates_and_existing_binding(member_bindings, has_binding):
    r = fact_runtime(member_bindings)
    if has_binding:
        ctx = context(r, "Q1")
        apply_memory(r, parse_memory("BOUND | Alice | F1", ctx), ctx)
        add_fact(r, "Cindy's teacher also teaches Bob.")
    ctx = context(r, "Q1")
    before = retained_state(r)
    binding_id = r.state.executions["Q1"].current_binding_id

    response = parse_memory(" \nNONE\t\n", ctx)
    assert response.operations[0].binding_result.state == "NOOP"
    receipt = apply_memory(r, response, ctx)

    assert receipt["changed"] is False
    assert retained_state(r) == before
    assert r.state.executions["Q1"].current_binding_id == binding_id
    assert not any(event.event_type == "BINDING_RETIRED"
                   for event in r.store.list_runtime_events(r.run_id))


def test_normalization_audit_preserves_raw_reply_and_is_idempotent_after_resume():
    r = fact_runtime(True)
    ctx = context(r, "Q1")
    raw = " \n NONE\t\n"
    response = parse_memory(raw, ctx)
    receipt = apply_memory(r, response, ctx)
    events = r.store.list_runtime_events(r.run_id)
    normalized = [event for event in events if event.event_type == "MEMORY_FORMAT_NORMALIZED"]
    assert len(normalized) == 1
    assert normalized[0].payload == dict(
        query_id="Q1", review_id=ctx.review_id, context_id=ctx.context_id, phase="FINAL",
        original_response=raw, normalized_response="NOOP", reason="STANDALONE_NONE_AS_NOOP")
    assert not any(event.event_type == "MEMORY_LINES_IGNORED" for event in events)
    assert replay_events(events).export() == r.export()

    restored = RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive,
                         store=r.store, config=r.config)
    saved_response = MemoryResponseR2.model_validate(response.model_dump(mode="json"))
    assert apply_memory(restored, saved_response, ctx) == receipt
    assert restored.store.list_runtime_events(r.run_id) == events


@pytest.mark.parametrize("member_bindings", [False, True])
@pytest.mark.parametrize("raw", [
    "NONE\nBOUND | Alice | F1", "BOUND | Alice | F1\nNONE", "NONE\nNONE",
    "NONE\nNOOP", "NONE | no evidence", "none", "```\nNONE\n```",
])
def test_none_compatibility_never_accepts_mixed_or_nonliteral_replies(member_bindings, raw):
    r = fact_runtime(member_bindings)
    ctx = context(r, "Q1")
    with pytest.raises(ValueError):
        parse_memory(raw, ctx)


@pytest.mark.parametrize("member_bindings", [False, True])
def test_none_in_support_field_is_not_a_noop(member_bindings):
    r = fact_runtime(member_bindings)
    ctx = context(r, "Q1")
    response = parse_memory("BOUND | Alice | NONE", ctx)
    result = response.operations[0].binding_result
    assert result.state == "BOUND" and result.value == "Alice"
    assert result.support_fact_ids == []
    assert response.format_normalizations == []
    # Normalization must not bypass existing proof requirements either.
    with pytest.raises(ValueError, match="MISSING_(MEMBER|BINDING)_PROOF"):
        apply_memory(r, response, ctx)


@pytest.mark.parametrize("member_bindings", [False, True])
def test_raw_review_and_final_do_not_accept_none_alias(member_bindings):
    r = fixture(plan=member_plan(chain()) if member_bindings else chain())
    ingest(r, [("Q1", "D0:S0")])
    ctx = context(r, "Q1")
    assert ctx.phase == "REVIEW" and not ctx.fact_only
    with pytest.raises(ValueError, match="INVALID_R2_MEMORY_FIELD:NONE"):
        parse_memory("NONE", ctx)
    ctx = review_all(r, "Q1")
    with pytest.raises(ValueError, match="INVALID_R2_MEMORY_FIELD:NONE"):
        parse_memory("NONE", ctx)


def test_ordinary_response_keeps_pre_normalization_receipt_hash():
    r = fact_runtime(True)
    ctx = context(r, "Q1")
    response = parse_memory("NOOP", ctx)
    legacy_encoded = response.model_dump(mode="json", exclude={"format_normalizations"})
    receipt = apply_memory(r, response, ctx)
    assert r.store.r2_receipt(r.run_id, ctx.context_id, digest(legacy_encoded)) == receipt
    assert apply_memory(r, MemoryResponseR2.model_validate(legacy_encoded), ctx) == receipt
