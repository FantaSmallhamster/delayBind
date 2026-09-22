"""R2 structural acceptance: no API, no heuristic claims about entailment."""
import sqlite3
from dataclasses import replace
import pytest

from delaybind_core.archive import SentenceArchive
from delaybind_core.cursor_v52 import TextReadCursor
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import MemoryResponseR2, BindingResult, fact_id
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.context_r2 import build_update_context, build_memory_context, persist_memory_context, evidence_pack
from delaybind_core.protocol_r2 import parse_update, parse_memory, RepairScope
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.navigation_r2 import binding_effective, current_uses, assert_invariants, query_ready
from delaybind_core.navigation_r2 import query_projection
from delaybind_core.review_jobs_r2 import next_job, ensure_review
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.token_budget import TokenCounter
from delaybind_core.replay import replay_events
from delaybind_core.prompts_r2 import messages
from test_v52_sources import CharacterTokenizer

TEXT = "Cindy's teacher is Alice. Alice's mother is Mary. Mary was born in Suzhou. Cindy's teacher is Bob. Bob's mother is June. June was born in Beijing. Other Cindy likes Alice."


def chain():
    return QueryPlanV3(plan_id="p", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who is ?teacher's mother?", output="?mother", inputs={"?teacher": "Q1"}),
        dict(id="Q3", template="Where was ?mother born?", output="?city", inputs={"?mother": "Q2"})])


def fixture(*, plan=None, text=TEXT, config=None, store=None):
    store = store or SQLiteEventStore()
    archive = SentenceArchive(store, "r")
    r = RuntimeR2(run_id="r", plan=plan or chain(), archive=archive, store=store,
                  config=config or RunnerConfig(protocol_version="v5.2-r2"))
    cursor = TextReadCursor(text, archive, sample_id="s", counter=TokenCounter(CharacterTokenizer()))
    window = cursor.next_anchored_window(10000)
    r.record_window(window, cursor.state())
    return r


def test_fact_only_update_prompt_is_english_high_recall_and_hides_runtime_state():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    ctx = build_update_context(r, "Where was Cindy's teacher's mother born?", ["D0:S0"])
    system, user = (item["content"] for item in messages("UPDATE", ctx))

    assert "constrained fact-extraction interface" in system
    assert "Favor recall" in user
    assert "unambiguous equivalent or inverse expression" in user
    assert 'source text "Georg is the son of Jobst" must be routed' in user
    assert '"Film X was directed by Ada and Bea"' in user
    assert "Extraction targets:" in user and "Current window:" in user
    assert user.count("Q1 | Who teaches Cindy?") == 1
    assert "Cindy's teacher is Alice." in user

    for hidden in ("[ACTIVE]", "[DORMANT]", "cardinality:", "requires_complete_set:",
                   "binding effective:", "Working memory:", "Routing checklist:"):
        assert hidden not in user


def ingest(r, routes):
    """routes: (qid, source ref, optional text). One original frozen window."""
    ctx = build_update_context(r, "Question?", [row[1] for row in routes])
    lines, fids = [], []
    for row in routes:
        qid, ref = row[:2]
        text = row[2] if len(row) > 2 else r.archive.fetch_sentence(ref).text.strip()
        lines.append(f"{qid} | {ref} | {text}")
        fids.append(fact_id(text, [ref]))
    update, errors = parse_update("\n".join(lines), ctx)
    assert not errors
    r.ingest(update, ctx)
    return fids


def context(r, qid):
    session = next(rv for rv in r.state.reviews.values() if rv.query_id == qid and rv.status not in {"DONE", "CANCELLED"})
    ctx = build_memory_context(r, session.review_id)
    persist_memory_context(r, ctx)
    return ctx


def proposal(ctx, reviews=(), result=None):
    return MemoryResponseR2(context_id=ctx.context_id, review_id=ctx.review_id, operations=[dict(
        op=ctx.allowed_mode, query_id=ctx.query_id, evidence_reviews=list(reviews), binding_result=result)])


def review_all(r, qid, verdicts=None):
    verdicts = verdicts or {}
    while True:
        ctx = context(r, qid)
        if ctx.phase == "FINAL":
            return ctx
        reviews = []
        for fid in ctx.required_reviews:
            spec = verdicts.get(fid, "ACCEPT")
            item = dict(fact_id=fid, verdict=spec if isinstance(spec, str) else spec["verdict"],
                        checked_refs=list(r.state.facts[fid].source_refs), reason_code="TEST", reason="Fixture raw decision")
            if isinstance(spec, dict):
                item.update(spec)
            reviews.append(item)
        apply_memory(r, proposal(ctx, reviews), ctx)


def finalize(r, qid, value, supports, *, verdicts=None, kind="DIRECT", decision_refs=None):
    ctx = review_all(r, qid, verdicts)
    result = dict(state="UNBOUND" if value is None else "BOUND", value=value, kind=kind,
                  support_fact_ids=supports, decision_source_refs=decision_refs or [], reason_code="TEST", reason="Fixture decision")
    p = proposal(ctx, result=result)
    receipt = apply_memory(r, p, ctx)
    assert_invariants(r.state)
    return ctx, p, receipt


def recall_all(r, qid):
    while True:
        job = next((j for j in r.state.jobs.values() if j.kind == "RECALL" and j.target_query == qid and j.status == "PENDING"), None)
        if not job:
            return
        batch = job.payload["candidates"][job.payload["offset"]:job.payload["offset"] + r.config.candidate_batch_size]
        r.finish_recall_batch(job.job_id, batch)


def bound_chain(r):
    a, b, c = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1"), ("Q3", "D0:S2")])
    finalize(r, "Q1", "Alice", [a])
    recall_all(r, "Q2")
    finalize(r, "Q2", "Mary", [b])
    recall_all(r, "Q3")
    finalize(r, "Q3", "Suzhou", [c])
    return a, b, c


def test_p01_p02_update_routes_are_snapshot_exact_and_dormant_is_neutral():
    r = fixture()
    a, b = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1")])
    assert current_uses(r.state, "Q1")[0].status == "PENDING"
    assert current_uses(r.state, "Q2")[0].status == "CANDIDATE"
    assert not r.export()["bindings"]
    ctx = build_update_context(r, "?", ["D0:S0"])
    good, bad = parse_update("Q1 | D0:S0 | x\nQ9 | D0:S0 | y", ctx)
    assert good.facts[0].routes[0].observed_status == "ACTIVE" and len(bad) == 1
    assert r.state.route_index == {"Q1": [a], "Q2": [b]}


def test_r2_update_is_three_fields_and_runtime_attaches_snapshot_status():
    r = fixture()
    ctx = build_update_context(r, "?", ["D0:S1"])
    update, rejected = parse_update("Q2 | D0:S1 | Alice's mother is Mary.", ctx)
    assert not rejected
    assert update.facts[0].routes[0].observed_status == "DORMANT"
    legacy, rejected = parse_update("Q2 | D0:S1 | Alice's mother is Mary. | DORMANT", ctx)
    assert not legacy.facts
    assert rejected[0]["error"] == "ONE_QUERY_ROUTE_PER_LINE_REQUIRED"


def test_fact_only_update_and_memory_have_no_raw_ids_or_review_phase():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    ctx = build_update_context(r, "?", ["D0:S0"])
    assert ctx["fact_only"] and ctx["visible_sources"] == [] and ctx["working_memory"]["raw_evidence"] == []
    update, rejected = parse_update("Q1 | Cindy's teacher is Alice.", ctx)
    assert not rejected and update.facts[0].source_refs == []
    wrong, rejected = parse_update("Q1 | D0:S0 | Cindy's teacher is Alice.", ctx)
    assert not wrong.facts and rejected[0]["error"] == "ONE_QUERY_ROUTE_PER_LINE_REQUIRED"
    empty, rejected = parse_update("Q1 | NONE", ctx)
    assert not empty.facts and not rejected
    r.ingest(update, ctx)
    memory_ctx = context(r, "Q1")
    fid = fact_id("Cindy's teacher is Alice.", [])
    assert memory_ctx.fact_only and memory_ctx.phase == "FINAL"
    assert memory_ctx.required_reviews == [] and memory_ctx.visible_source_refs == []
    parsed = parse_memory(f"BOUND | Alice | {fid}", memory_ctx)
    apply_memory(r, parsed, memory_ctx)
    assert r.export()["bindings"] == {"?teacher": "Alice"}
    assert r.state.review_records == {}
    assert r.state.binding_store["B1"].source_refs == []


def test_fact_only_memory_prompt_is_decision_focused_and_short_alias_maps_to_durable_fact():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    update_ctx = build_update_context(r, "Who teaches Cindy?", ["D0:S0"])
    update, rejected = parse_update("Q1 | Cindy's teacher is Alice.", update_ctx)
    assert not rejected
    r.ingest(update, update_ctx)
    memory_ctx = context(r, "Q1")
    fid = next(iter(r.state.facts))
    payload = {
        "question": "Who teaches Cindy?",
        **memory_ctx.model_dump(mode="json"),
        "query_instance": next(q for q in query_projection(r.state)["queries"] if q["id"] == "Q1"),
        "old_binding": None,
        "staged_reviews": [],
    }
    body = messages("MEMORY", payload)[-1]["content"]
    input_data = body.split("Input data:\n", 1)[1]
    assert "Eligible facts:\nF1 | Cindy's teacher is Alice." in body
    assert "Decision authorized:" not in body
    assert "Fact uses:" not in input_data and "PENDING" not in input_data
    assert "Barriers:" not in input_data and "Scope closed:" not in input_data
    assert fid not in body
    with pytest.raises(ValueError, match="UNKNOWN_FACT_ALIAS"):
        parse_memory("BOUND | Alice | F9", memory_ctx)
    with pytest.raises(ValueError, match="DUPLICATE_SUPPORT_FACT"):
        parse_memory("BOUND | Alice | F1,F1", memory_ctx)
    parsed = parse_memory("BOUND | Alice | F1", memory_ctx)
    assert parsed.operations[0].binding_result.support_fact_ids == [fid]
    apply_memory(r, parsed, memory_ctx)
    assert r.export()["bindings"] == {"?teacher": "Alice"}


def test_fact_only_update_rejects_multiline_non_atomic_fact():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    ctx = build_update_context(r, "?", ["D0:S0"])
    update, rejected = parse_update(r"Q1 | Cindy's teacher is Alice.\nAlice lives in Paris.", ctx)
    assert not update.facts
    assert rejected[0]["error"] == "FACT_MUST_BE_ONE_ATOMIC_LINE"


def test_fact_only_single_rejects_joined_people_but_accepts_single_conjunctive_name():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    ctx = build_update_context(r, "?", ["D0:S0"])
    update, _ = parse_update("Q1 | The film was directed by Vladimir Strizhevsky.", ctx)
    r.ingest(update, ctx)
    memory_ctx = context(r, "Q1")
    fid = next(iter(r.state.facts))
    with pytest.raises(ValueError, match="SINGLE_VALUE_CONTAINS_MULTIPLE_VALUES"):
        parse_memory(f"BOUND | Vladimir Strizhevsky and Joseph N. Ermolieff | {fid}", memory_ctx)
    parsed = parse_memory(f"BOUND | Trinidad and Tobago | {fid}", memory_ctx)
    assert parsed.operations[0].binding_result.value == "Trinidad and Tobago"


def test_fact_only_memory_rejects_raw_first_wire_shapes():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    ctx = build_update_context(r, "?", ["D0:S0"])
    update, _ = parse_update("Q1 | Cindy's teacher is Alice.", ctx)
    r.ingest(update, ctx)
    memory_ctx = context(r, "Q1")
    fid = next(iter(r.state.facts))
    with pytest.raises(ValueError, match="FACT_ONLY_BOUND_FIELD_COUNT"):
        parse_memory(f"BOUND | Alice | {fid} | D0:S0", memory_ctx)
    with pytest.raises(ValueError, match="FACT_ONLY_NOOP_FIELD_COUNT"):
        parse_memory("UNBOUND | D0:S0", memory_ctx)


def test_fact_only_noop_changes_neither_candidates_nor_binding_state():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    update_ctx = build_update_context(r, "?", ["D0:S0"])
    update, _ = parse_update("Q1 | Cindy's teacher may be Alice.", update_ctx)
    r.ingest(update, update_ctx)
    memory_ctx = context(r, "Q1")
    before = {key: use.status for key, use in r.state.uses.items()}
    receipt = apply_memory(r, parse_memory("NOOP", memory_ctx), memory_ctx)
    assert receipt["changed"] is False
    assert {key: use.status for key, use in r.state.uses.items()} == before
    assert r.state.executions["Q1"].current_binding_id is None
    assert r.state.diagnostics[-1]["state"] == "NOOP"
    assert not any(e.event_type == "BINDING_RETIRED" for e in r.store.list_runtime_events("r"))


def test_fact_only_bound_keeps_unselected_facts_as_candidates_not_rejected():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    update_ctx = build_update_context(r, "?", ["D0:S0"])
    update, _ = parse_update("Q1 | Cindy's teacher is Alice.\nQ1 | Cindy's teacher is Bob.", update_ctx)
    r.ingest(update, update_ctx)
    by_text = {fact.text: fid for fid, fact in r.state.facts.items()}
    memory_ctx = context(r, "Q1")
    alice = by_text["Cindy's teacher is Alice."]
    apply_memory(r, parse_memory(f"BOUND | Alice | {alice}", memory_ctx), memory_ctx)
    statuses = {r.state.facts[use.fact_id].text: use.status for use in current_uses(r.state, "Q1")}
    assert statuses == {"Cindy's teacher is Alice.": "ACCEPTED", "Cindy's teacher is Bob.": "CANDIDATE"}


def test_p03_multi_query_fact_is_immutable_but_uses_independent():
    plan = QueryPlanV3(plan_id="p", queries=[dict(id=f"Q{i}", template=f"Need {i}?", output=f"?x{i}") for i in (1, 2)])
    r = fixture(plan=plan)
    a, same = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S0")])
    assert a == same and len(r.state.facts) == 1
    finalize(r, "Q1", None, [], verdicts={a: "REJECT"})
    finalize(r, "Q2", "Alice", [a])
    assert {u.status for u in r.state.uses.values()} == {"REJECTED", "ACCEPTED"}


@pytest.mark.parametrize("bad", ["NONE", "{}", "[]", "```text\nNONE\n```", "ASSESS | Q1 | F1 | ACCEPT | D0:S0 | OK", "BIND | Q1", "RESULT | PENDING", "END BIND"])
def test_p18_text_contract_strictly_rejects_old_or_ambiguous_outputs(bad):
    r = fixture()
    ingest(r, [("Q1", "D0:S0")])
    with pytest.raises(ValueError):
        parse_memory(bad, context(r, "Q1"))


def test_p09_p10_p11_required_reviews_and_phase_cannot_be_skipped():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = context(r, "Q1")
    before = r.export()
    with pytest.raises(ValueError, match="REQUIRED_REVIEWS"):
        apply_memory(r, proposal(ctx), ctx)
    ignored = parse_memory(f"BOUND | Alice | DIRECT | {a} | D0:S0 | yes", ctx)
    assert ignored.ignored_lines[0]["reason"] == "RESULT_IGNORED_DURING_REVIEW"
    with pytest.raises(ValueError, match="REQUIRED_REVIEWS"):
        apply_memory(r, ignored, ctx)
    assert before == r.export()
    ctx = review_all(r, "Q1")
    with pytest.raises(ValueError, match="PHASE"):
        apply_memory(r, proposal(ctx), ctx)


def test_minimal_memory_wire_derives_runtime_fields_and_reason_codes():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    review_ctx = context(r, "Q1")
    review = parse_memory(f"REVIEW | {a} | ACCEPT | D0:S0 | exact raw support", review_ctx)
    action = review.operations[0]
    assert action.op == "BIND" and action.query_id == "Q1" and action.binding_result is None
    assert action.evidence_reviews[0].reason_code == "RAW_SUPPORTED"
    apply_memory(r, review, review_ctx)
    final_ctx = context(r, "Q1")
    final = parse_memory(f"BOUND | Alice | DIRECT | {a} | D0:S0 | exact raw support", final_ctx)
    assert final.operations[0].binding_result.reason_code == "SUPPORTED"
    apply_memory(r, final, final_ctx)
    assert r.export()["bindings"]["?teacher"] == "Alice"


def test_runtime_owned_memory_accepts_reasonless_review_and_polluted_legacy_source_tail():
    r = fixture()
    fid = ingest(r, [("Q1", "D0:S0")])[0]
    review_ctx = context(r, "Q1")
    parsed = parse_memory(f"REVIEW | {fid} | ACCEPT | D0:S0", review_ctx)
    assert parsed.operations[0].evidence_reviews[0].reason_code == "RAW_SUPPORTED"
    apply_memory(r, parsed, review_ctx)
    final_ctx = context(r, "Q1")
    legacy = parse_memory(
        f"BOUND | Alice | DIRECT | {fid} | D0:S0 explicitly states the answer", final_ctx)
    assert legacy.operations[0].binding_result.decision_source_refs == ["D0:S0"]
    apply_memory(r, legacy, final_ctx)
    assert r.export()["bindings"]["?teacher"] == "Alice"


def test_wrong_phase_result_is_ignored_but_valid_review_commits_and_is_audited():
    r = fixture()
    fid = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = context(r, "Q1")
    parsed = parse_memory(
        f"REVIEW | {fid} | ACCEPT | D0:S0\nBOUND | Alice | {fid} | D0:S0", ctx)
    apply_memory(r, parsed, ctx)
    assert r.state.reviews[ctx.review_id].staged_review_ids
    events = r.store.list_runtime_events("r")
    warning = next(e for e in events if e.event_type == "MEMORY_LINES_IGNORED")
    assert warning.payload["ignored_lines"][0]["reason"] == "RESULT_IGNORED_DURING_REVIEW"


def test_binding_activates_child_and_schedules_recall_from_saved_candidates():
    r = fixture()
    parent, child = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1")])
    finalize(r, "Q1", "Alice", [parent])
    assert r.state.executions["Q2"].status == "ACTIVE"
    job = next_job(r.state)
    assert job.kind == "RECALL" and job.target_query == "Q2" and child in job.payload["candidates"]
    activation = next(e for e in r.store.list_runtime_events("r") if e.event_type == "QUERY_ACTIVATED")
    assert activation.payload["query_id"] == "Q2"


@pytest.mark.parametrize("verdict,suffix,code", [
    ("REJECT", "", "REJECTED_BY_REVIEW"),
    ("HOLD", "\nCONTEXT | {fid} | D0:S0 | 0 | 1", "NEED_CONTEXT"),
    ("CONFLICT", "", "SOURCE_CONFLICT"),
])
def test_minimal_review_reason_codes_are_runtime_owned(verdict, suffix, code):
    r = fixture()
    fid = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = context(r, "Q1")
    raw = f"REVIEW | {fid} | {verdict} | D0:S0 | model reason" + suffix.format(fid=fid)
    parsed = parse_memory(raw, ctx)
    assert parsed.operations[0].evidence_reviews[0].reason_code == code


def test_minimal_correction_and_unbound_codes_are_runtime_owned():
    r = fixture()
    fid = ingest(r, [("Q1", "D0:S0", "Wrong")])[0]
    ctx = context(r, "Q1")
    corrected = parse_memory(
        f"REVIEW | {fid} | ACCEPT | D0:S0 | corrected from raw\n"
        f"CORRECTION | {fid} | N1 | D0:S0 | Cindy's teacher is Alice.", ctx)
    review = corrected.operations[0].evidence_reviews[0]
    assert review.reason_code == "CORRECTED" and review.correction.local_id == "N1"
    apply_memory(r, corrected, ctx)
    final_ctx = context(r, "Q1")
    unbound = parse_memory("UNBOUND | D0:S0 | not enough evidence", final_ctx)
    assert unbound.operations[0].binding_result.reason_code == "INSUFFICIENT_EVIDENCE"


def test_minimal_final_accepts_four_fields_legacy_shapes_and_ignores_repeated_reviews():
    r = fixture()
    fid = ingest(r, [("Q1", "D0:S0")])[0]
    ctx = review_all(r, "Q1")
    minimal = parse_memory(f"BOUND | Alice | {fid} | D0:S0", ctx)
    assert minimal.operations[0].binding_result.kind == "DIRECT"
    legacy = parse_memory(f"BOUND | Alice | DIRECT | {fid} | D0:S0 | ignored reason", ctx)
    assert legacy.operations[0].binding_result.support_fact_ids == [fid]
    repeated = parse_memory(f"REVIEW | {fid} | ACCEPT | D0:S0 | repeated\nUNBOUND | D0:S0 | no", ctx)
    assert repeated.ignored_lines[0]["reason"] == "REVIEW_IGNORED_DURING_FINAL"
    assert repeated.operations[0].binding_result.state == "UNBOUND"


@pytest.mark.parametrize("value", [False, 0, "Alice"])
def test_p12_valid_false_zero_and_text_are_not_unbound(value):
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    finalize(r, "Q1", value, [a])
    assert r.state.binding_store["B1"].value == value
    assert r.export()["bindings"]["?teacher"] == value


@pytest.mark.parametrize("value", [None, "", "NONE", "UNKNOWN", "?who", float("nan")])
def test_bound_placeholders_rejected(value):
    with pytest.raises(ValueError):
        BindingResult(state="BOUND", value=value, reason_code="TEST", reason="x")


def test_p13_collection_waits_for_scope_then_retries():
    plan = QueryPlanV3(plan_id="p", queries=[dict(id="Q1", template="All teachers?", output="?people", cardinality="SET", requires_complete_set=True)])
    r = fixture(plan=plan)
    a = ingest(r, [("Q1", "D0:S0")])[0]
    review_all(r, "Q1")
    session = next(iter(r.state.reviews.values()))
    assert session.status == "WAITING_SCOPE"
    assert next(j for j in r.state.jobs.values() if j.review_id == session.review_id).status == "WAITING_SCOPE"
    r.close_scope()
    ctx = context(r, "Q1")
    result = dict(state="BOUND", value=["Alice", "Alice"], kind="DIRECT", support_fact_ids=[a],
                  decision_source_refs=["D0:S0"], reason_code="TEST", reason="Fixture decision")
    apply_memory(r, proposal(ctx, result=result), ctx)
    assert r.export()["bindings"] == {"?people": ["Alice"]}


def test_fact_only_collection_waits_for_scope_without_a_review_call():
    plan = QueryPlanV3(plan_id="p", queries=[dict(
        id="Q1", template="All teachers?", output="?people",
        cardinality="SET", requires_complete_set=True)])
    r = fixture(plan=plan, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    update_ctx = build_update_context(r, "?", ["D0:S0"])
    update, rejected = parse_update("Q1 | Cindy's teacher is Alice.", update_ctx)
    assert not rejected
    r.ingest(update, update_ctx)
    session = next(iter(r.state.reviews.values()))
    job = next(j for j in r.state.jobs.values() if j.review_id == session.review_id)
    assert session.status == job.status == "WAITING_SCOPE"
    assert session.context_ids == []
    r.close_scope()
    ctx = context(r, "Q1")
    fid = next(iter(r.state.facts))
    apply_memory(r, parse_memory(f'BOUND | ["Alice"] | {fid}', ctx), ctx)
    assert r.export()["bindings"] == {"?people": ["Alice"]}


def test_d01_d05_d14_reaffirm_restores_chain_without_recalling_again():
    r = fixture()
    a, b, c = bound_chain(r)
    bindings = r.export()["bindings"]
    old_jobs = set(r.state.jobs)
    new = ingest(r, [("Q1", "D0:S6")])[0]
    assert r.state.executions["Q1"].status == "RESOLVED"
    assert all(not binding_effective(r.state, bid) for bid in ("B1", "B2", "B3"))
    assert not evidence_pack(r).navigation["facts"]
    assert all(r.state.binding_store[b].valid for b in ("B1", "B2", "B3"))
    ctx = review_all(r, "Q1", {new: "REJECT"})
    assert r.state.uses[r.state.binding_store["B1"].direct_use_ids[0]].status == "ACCEPTED"
    finalize(r, "Q1", "Alice", [a])
    assert r.export()["bindings"] == bindings and len(r.state.binding_store) == 3
    assert not [j for jid, j in r.state.jobs.items() if jid not in old_jobs and j.kind == "RECALL"]
    assert any(e.event_type == "BINDING_REAFFIRMED" for e in r.store.list_runtime_events("r"))


@pytest.mark.parametrize("same_value", [False, True])
def test_d02_d04_d06_d07_replacement_invalidates_descendants_even_same_value(same_value):
    r = fixture()
    a, b, c = bound_chain(r)
    old_uses = [u.use_id for u in r.state.uses.values() if u.status == "ACCEPTED" and u.query_id != "Q1"]
    new = ingest(r, [("Q1", "D0:S3")])[0]
    finalize(r, "Q1", "Alice" if same_value else "Bob", [new])
    assert r.state.binding_store["B4"].binding_revision == 2
    assert all(not r.state.binding_store[k].valid for k in ("B1", "B2", "B3"))
    assert all(r.state.uses[k].status == "INVALIDATED" for k in old_uses)
    assert not r.state.navigation_links
    assert b in r.state.route_index["Q2"] and c in r.state.route_index["Q3"]
    assert next(u for u in current_uses(r.state, "Q1") if u.fact_id == a).status == "ACCEPTED"
    assert r.state.executions["Q3"].status == "DORMANT"
    assert next_job(r.state).kind == "RECALL"
    assert r.state.query_edges == r.state.binding_ports


def test_d03_d18_unbound_retires_chain_preserves_raw_and_retry_gate():
    r = fixture()
    a, b, c = bound_chain(r)
    new = ingest(r, [("Q1", "D0:S3")])[0]
    finalize(r, "Q1", None, [], verdicts={a: "CONFLICT", new: "CONFLICT"}, decision_refs=["D0:S0", "D0:S3"])
    assert not r.export()["bindings"]
    assert [r.state.executions[f"Q{i}"].status for i in (1, 2, 3)] == ["ACTIVE", "DORMANT", "DORMANT"]
    assert {x.source_ref for x in evidence_pack(r).raw_evidence} == {"D0:S0", "D0:S3"}
    clone = r.state.model_copy(deep=True)
    assert ensure_review(clone, "Q1", "RETRY", []) is None
    assert a in r.state.facts and b in r.state.facts and c in r.state.facts


def test_r04_stale_context_is_not_repairable_and_r05_r06_receipts():
    r = fixture()
    a = ingest(r, [("Q1", "D0:S0")])[0]
    old = context(r, "Q1")
    ingest(r, [("Q1", "D0:S3")])
    with pytest.raises(ValueError, match="STALE_MEMORY_CONTEXT"):
        apply_memory(r, proposal(old), old)
    ctx, p, receipt = finalize(r, "Q1", "Alice", [a])
    before = r.export()
    assert apply_memory(r, p, ctx) == receipt and before == r.export()
    p.operations[0].binding_result.value = "Bob"
    with pytest.raises(ValueError, match="CONTEXT_CONSUMED"):
        apply_memory(r, p, ctx)


def test_r08_all_recall_batches_finish_before_review_and_final_raw_contains_conflict():
    r = fixture(config=RunnerConfig(protocol_version="v5.2-r2", candidate_batch_size=1))
    a, b, c = ingest(r, [("Q1", "D0:S0"), ("Q2", "D0:S1"), ("Q2", "D0:S4")])
    finalize(r, "Q1", "Alice", [a])
    j = next_job(r.state)
    r.finish_recall_batch(j.job_id, [j.payload["candidates"][0]])
    assert not [rv for rv in r.state.reviews.values() if rv.query_id == "Q2"]
    recall_all(r, "Q2")
    ctx = review_all(r, "Q2", {c: "CONFLICT"})
    assert ctx.phase == "FINAL" and {"D0:S0", "D0:S1", "D0:S4"} <= set(ctx.visible_source_refs)
    with pytest.raises(ValueError, match="UNRESOLVED_REVIEW"):
        finalize(r, "Q2", "Mary", [b])
    finalize(r, "Q2", None, [])


def test_p17_same_window_registration_order_is_irrelevant():
    a, b = fixture(), fixture()
    rows = [("Q1", "D0:S0"), ("Q2", "D0:S1"), ("Q3", "D0:S2")]
    ingest(a, rows)
    ingest(b, list(reversed(rows)))
    assert a.state.route_index == b.state.route_index
    assert a.state.uses == b.state.uses
    assert a.state.reviews == b.state.reviews


def test_p04_repair_freezes_original_claims_and_keeps_valid_items():
    r = fixture()
    ctx = build_update_context(r, "?", ["D0:S0"])
    update, rejected = parse_update("Q1 | D0:S0 | good\nQ99 | D0:S0 | fix", ctx)
    scope = RepairScope(rejected, update)
    r.ingest(update, ctx)
    repaired, _ = parse_update("Q1 | D0:S0 | good\nQ1 | D0:S0 | fix\nQ1 | D0:S0 | novel", ctx)
    fixed, errors = scope.restrict(repaired)
    assert [f.text for f in fixed.facts] == ["fix"] and len(errors) == 1
    r.ingest(fixed, ctx)
    assert {f.text for f in r.state.facts.values()} == {"good", "fix"}


def test_r11_replay_including_pending_barriers_and_resume():
    r = fixture()
    bound_chain(r)
    ingest(r, [("Q1", "D0:S3")])
    review_all(r, "Q1")
    replayed = replay_events(r.store.list_runtime_events("r"))
    assert replayed.export() == r.export()
    restored = RuntimeR2(run_id="r", plan=r.state.plan, archive=SentenceArchive(r.store, "r"), store=r.store, config=r.config)
    assert restored.export() == r.export()
    assert restored.archive.watermark == r.state.read_watermark
    assert next_job(restored.state) is not None


def test_p03_one_fact_routes_to_active_resolved_and_dormant_independently():
    plan = chain()
    plan = QueryPlanV3(plan_id="p", queries=[*plan.queries, dict(id="Q4", template="Independent?", output="?other")])
    r = fixture(plan=plan)
    a = ingest(r, [("Q1", "D0:S0")])[0]
    finalize(r, "Q1", "Alice", [a])
    x, y, z = ingest(r, [("Q1", "D0:S3"), ("Q4", "D0:S3"), ("Q3", "D0:S3")])
    assert x == y == z
    assert len([f for f in r.state.facts.values() if f.fact_id == x]) == 1
    statuses = {u.query_id: u.status for u in r.state.uses.values() if u.fact_id == x}
    assert statuses == {"Q1": "PENDING", "Q3": "CANDIDATE", "Q4": "PENDING"}
    assert {r.state.reviews[j.review_id].mode for j in r.state.jobs.values() if j.kind == "MEMORY" and j.status == "PENDING"} >= {"BIND", "REBIND"}


@pytest.mark.parametrize("value", ['{"a": 1, "a": 2}', '[{"a":1,"a":2}]', "NaN", "Infinity", "[broken]"])
def test_typed_value_duplicate_keys_and_nonfinite_literals_are_not_tolerated(value):
    from delaybind_core.protocol_r2 import typed_value
    with pytest.raises(ValueError):
        typed_value(value)
