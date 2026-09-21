import pytest
from test_r2_core import fixture, ingest, context, proposal, review_all, finalize, chain
from delaybind_core.protocol_r2 import parse_update
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.context_r2 import build_update_context, build_memory_context, persist_memory_context, budgeted_evidence_pack
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.review_jobs_r2 import next_job
from delaybind_core.schema_r2 import UpdateResponseR2, fact_id
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.runner import RunnerConfig
from delaybind_core.archive import SentenceArchive, FutureSourceAccessError
from delaybind_core.cursor_v52 import TextReadCursor
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.token_budget import TokenCounter
from test_v52_sources import CharacterTokenizer


@pytest.mark.parametrize("ref", ["F1", "Q1", "D0", "D0:S99", "D99:S0"])
def test_p05_bad_sources_rejected_locally_without_discarding_valid_fact(ref):
    r = fixture()
    ctx = build_update_context(r, "?", ["D0:S0"])
    update, rejected = parse_update(f"Q1 | D0:S0 | Good\nQ1 | {ref} | Bad", ctx)
    assert len(update.facts) == len(rejected) == 1
    r.ingest(update, ctx)
    assert {f.text for f in r.state.facts.values()} == {"Good"}


def test_p06_read_but_not_visible_and_p08_correction_rollback():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = context(r, "Q1")
    assert "D0:S1" not in ctx.visible_source_refs
    assert r.archive.fetch_sentence("D0:S1")
    before = r.export()
    review = dict(fact_id=a, verdict="ACCEPT", checked_refs=["D0:S0", "D0:S1"], reason_code="TEST", reason="test")
    with pytest.raises(ValueError):
        apply_memory(r, proposal(ctx, [review]), ctx)
    review.update(checked_refs=["D0:S0"], correction=dict(local_id="N1", text="Wrong", source_refs=["D0:S1"]))
    with pytest.raises(ValueError):
        apply_memory(r, proposal(ctx, [review]), ctx)
    assert r.export() == before and r.store.latest_v52_state("r") == r.state.model_dump(mode="json")


def test_p07_correction_local_id_and_raw_immutability():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0", "Bob teaches Cindy.")])[0]
    ctx = review_all(r, "Q1")
    review = dict(fact_id=a, verdict="ACCEPT", checked_refs=["D0:S0"], reason_code="CORRECTED", reason="raw says Alice",
                  correction=dict(local_id="N1", text="Alice teaches Cindy.", source_refs=["D0:S0"]))
    result = dict(state="BOUND", value="Alice", support_fact_ids=["N1"], reason_code="SUPPORTED", reason="corrected source")
    apply_memory(r, proposal(ctx, [review], result), ctx)
    assert r.state.facts[a].text == "Bob teaches Cindy."
    assert "Alice" in r.archive.fetch_sentence("D0:S0").text
    assert r.state.binding_store["B1"].direct_fact_ids == [fact_id("Alice teaches Cindy.", ["D0:S0"])]


def test_p12_direct_empty_support_rejected_inferred_requires_declared_parents_and_intermediate():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    with pytest.raises(ValueError, match="MISSING_BINDING_PROOF"):
        finalize(r, "Q1", "Alice", [])
    with pytest.raises(ValueError, match="MISSING_BINDING_PROOF"):
        finalize(r, "Q1", "Alice", [], kind="INFERRED")
    finalize(r, "Q1", "Alice", [a])
    ctx = context(r, "Q2")
    assert ctx.phase == "FINAL" and "D0:S0" in ctx.visible_source_refs
    finalize(r, "Q2", "Mary", [], kind="INFERRED")
    assert r.state.binding_store["B2"].parent_binding_ids == ["B1"]
    with pytest.raises(ValueError, match="INFERRED_ONLY_FOR_INTERMEDIATE"):
        finalize(r, "Q3", "Suzhou", [], kind="INFERRED")


def test_p15_bounded_context_expansion_has_new_permissions_not_mutated_old_context():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    old = context(r, "Q1")
    review = dict(fact_id=a, verdict="HOLD", checked_refs=["D0:S0"], reason_code="NEED_CONTEXT", reason="test",
                  context_request=dict(anchor="D0:S0", before=0, after=1))
    apply_memory(r, proposal(old, [review]), old)
    job = next_job(r.state)
    assert job.kind == "CONTEXT_EXPAND"
    r.expand_context(job.job_id)
    expanded = context(r, "Q1")
    assert expanded.context_id != old.context_id
    assert "D0:S1" in expanded.visible_source_refs and "D0:S2" not in expanded.visible_source_refs
    assert "D0:S1" not in old.visible_source_refs
    assert r.state.context_expansions == 1
    assert expanded.inbox_revision > old.inbox_revision


def test_p14_fragment_hold_waits_for_future_complete_sentence_without_early_access():
    text = "Mary " + "really " * 20 + "was born in Suzhou."
    store = SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    r = RuntimeR2(run_id="r", plan=chain(), archive=archive, store=store,
                  config=RunnerConfig(protocol_version="v5.2-r2"))
    cursor = TextReadCursor(text, archive, sample_id="s", counter=TokenCounter(CharacterTokenizer()), window_mode="fragment")
    first = cursor.next_anchored_window(65)
    r.record_window(first, cursor.state())
    ref = first.source_refs[0]
    assert ":P" in ref
    with pytest.raises((FutureSourceAccessError, KeyError, ValueError)):
        archive.fetch_sentence("D0:S0")
    a = ingest(r, [("Q1", ref, "Incomplete navigation")])[0]
    ctx = context(r, "Q1")
    # Compact prompts omit complete=false; the archive still enforces it.
    accepted = dict(fact_id=a, verdict="ACCEPT", checked_refs=[ref], reason_code="TEST", reason="test")
    before = r.export()
    with pytest.raises(ValueError, match="INCOMPLETE_SOURCE_REQUIRE_CORRECTION"):
        apply_memory(r, proposal(ctx, [accepted]), ctx)
    assert r.export() == before
    hold = dict(fact_id=a, verdict="HOLD", checked_refs=[ref], reason_code="NEED_CONTEXT", reason="incomplete",
                context_request=dict(anchor=ref, before=0, after=0))
    apply_memory(r, proposal(ctx, [hold]), ctx)
    r.expand_context(next_job(r.state).job_id)
    assert r.state.reviews[ctx.review_id].status == "WAITING_CONTEXT"
    assert next_job(r.state) is None
    while not cursor.exhausted:
        w = cursor.next_anchored_window(65)
        r.record_window(w, cursor.state())
        r.expand_context(next_job(r.state).job_id)
    expanded = context(r, "Q1")
    assert "D0:S0" in expanded.visible_source_refs and expanded.phase == "REVIEW"
    assert archive.fetch_sentence("D0:S0").text == text
    fixed = dict(fact_id=a, verdict="ACCEPT", checked_refs=[ref, "D0:S0"], reason_code="COMPLETED", reason="full sentence",
                 correction=dict(local_id="N1", text="Mary was born in Suzhou.", source_refs=["D0:S0"]))
    apply_memory(r, proposal(expanded, [fixed]), expanded)
    finalize(r, "Q1", "Suzhou", [fact_id("Mary was born in Suzhou.", ["D0:S0"])])
    assert r.state.binding_store["B1"].source_refs == ["D0:S0"]


def test_hold_at_eof_can_finalize_unbound_and_does_not_spin():
    r = fixture(text="She went there.")
    a = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = context(r, "Q1")
    hold = dict(fact_id=a, verdict="HOLD", checked_refs=["D0:S0"], reason_code="NEED_CONTEXT", reason="unclear",
                context_request=dict(anchor="D0:S0", before=0, after=1))
    apply_memory(r, proposal(ctx, [hold]), ctx)
    r.expand_context(next_job(r.state).job_id)
    assert next_job(r.state) is None
    r.close_scope()
    finalize(r, "Q1", None, [], decision_refs=["D0:S0"])
    assert next_job(r.state) is None and not r.export()["bindings"]


def test_budgeted_view_can_hide_optional_history_but_keeps_raw_proof_and_storage():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", memory_char_budget=2500))
    a, b = ingest(r, [("Q1", "D0:S0"), ("Q1", "D0:S1", "Optional " + "x" * 4000)])
    finalize(r, "Q1", "Alice", [a])
    s = r.state.model_copy(deep=True)
    s.read_watermark += 1
    r.commit(s, [("TEST_PROGRESS", {})])
    before = r.export()
    pack = budgeted_evidence_pack(r, TokenCounter(CharacterTokenizer()))
    assert pack.navigation["view_hidden_fact_ids"] == [b]
    assert a in {n["fact_id"] for n in pack.navigation["facts"]}
    assert "D0:S0" in {raw.source_ref for raw in pack.raw_evidence}
    assert before == r.export()
