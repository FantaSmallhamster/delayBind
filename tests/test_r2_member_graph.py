"""Cardinality-free member graphs: fan-out, joins, recovery, and real dispatch.

These are deterministic structural tests, not claims about model accuracy.
"""
import asyncio
import pytest

from delaybind_core.schema_r2 import EvidencePlanR2, member_plan, fact_id
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.context_r2 import build_update_context, evidence_pack
from delaybind_core.protocol_r2 import parse_update, parse_memory
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.member_graph_r2 import branches_for, MemberBranchLimit
from delaybind_core.navigation_r2 import assert_invariants
from delaybind_core.replay import replay_events
from delaybind_core.prompts_r2 import messages, MEMBER_MEMORY_PROMPT_VERSION
from test_r2_core import fixture, context, recall_all, review_all


def plan():
    return EvidencePlanR2(plan_id="members", queries=[
        dict(id="Q1", template="Which team does Jane lead?", output="?team"),
        dict(id="Q2", template="Who are the members of ?team?", output="?person", inputs={"?team": "Q1"}),
        dict(id="Q3", template="Where was ?person born?", output="?city", inputs={"?person": "Q2"}),
        dict(id="Q4", template="Where did ?person study?", output="?school", inputs={"?person": "Q2"}),
        dict(id="Q5", template="What connects ?city and ?school?", output="?link", inputs={"?city": "Q3", "?school": "Q4"}),
    ])


def runtime():
    return fixture(plan=plan(), config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))


def ingest(r, *items):
    ctx = build_update_context(r, "Team members and their backgrounds?", ["D0:S0"])
    update, errors = parse_update("\n".join(f"{qid} | {text}" for qid, text in items), ctx)
    assert not errors
    r.ingest(update, ctx)
    return [fact_id(text, []) for _, text in items]


def decide(r, qid, *members, raw=None):
    recall_all(r, qid)
    ctx = context(r, qid)
    output = raw if raw is not None else "\n".join(f"BOUND | {value} | {','.join(facts) or 'NONE'}" for value, facts in members)
    response = parse_memory(output, ctx)
    receipt = apply_memory(r, response, ctx)
    assert_invariants(r.state)
    return ctx, response, receipt


def seeded(r):
    team, alice, bob, paris, rome, sa, sb = ingest(r,
        ("Q1", "Jane leads Team Red."), ("Q2", "Team Red includes Alice."), ("Q2", "Team Red includes Bob."),
        ("Q3", "Alice was born in Paris."), ("Q3", "Bob was born in Rome."),
        ("Q4", "Alice studied at A College."), ("Q4", "Bob studied at B College."))
    decide(r, "Q1", ("Team Red", [team]))
    decide(r, "Q2", ("Alice", [alice]), ("Bob", [bob]))
    return team, alice, bob, paris, rome, sa, sb


def current(r, qid):
    return r.state.binding_store[r.state.executions[qid].current_binding_id]


def test_plan_has_no_cardinality_and_each_member_has_its_own_proof_and_shared_parent():
    r = runtime()
    team, alice, bob, *_ = seeded(r)
    assert "cardinality" not in str(r.state.plan.model_dump())
    parent = current(r, "Q1").members[0]
    members = current(r, "Q2").members
    assert {m.value for m in members} == {"Alice", "Bob"}
    assert all(m.parent_member_ids == [parent.member_id] for m in members)
    assert {m.value: m.direct_fact_ids for m in members} == {"Alice": [alice], "Bob": [bob]}
    graph = evidence_pack(r).navigation
    child_edges = [e for e in graph["member_edges"] if e.get("source_member_id") == parent.member_id]
    assert {e["target_member_id"] for e in child_edges} == {m.member_id for m in members}
    assert parent.direct_fact_ids == [team]


def test_downstream_is_individually_instantiated_and_join_does_not_cross_siblings():
    r = runtime()
    _, _, _, paris, rome, sa, sb = seeded(r)
    for qid, mapping in [("Q3", {"Alice": ("Paris", paris), "Bob": ("Rome", rome)}),
                         ("Q4", {"Alice": ("A College", sa), "Bob": ("B College", sb)})]:
        recall_all(r, qid)
        for _ in range(2):
            ctx = context(r, qid)
            person = ctx.branch.bound_inputs["?person"]
            value, fid = mapping[person]
            decide(r, qid, (value, [fid]))
    branches = branches_for(r.state, "Q5")
    assert {(b.bound_inputs["?city"], b.bound_inputs["?school"]) for b in branches} == {
        ("Paris", "A College"), ("Rome", "B College")}
    assert len(branches) == 2
    # The original navigation links must also be precise, not just the new
    # member_edges view. Paris cannot inherit Bob's membership fact.
    paris_links = [e for e in r.state.navigation_links if e["target_fact_id"] == paris]
    alice_member = next(m for m in current(r, "Q2").members if m.value == "Alice")
    assert len(paris_links) == 1 and paris_links[0]["source_member_id"] == alice_member.member_id
    assert {r.state.uses[u].fact_id for u in paris_links[0]["source_use_ids"]} == set(alice_member.direct_fact_ids)


def test_partial_branch_commit_is_not_published_and_resume_replays_exactly():
    r = runtime()
    *_, paris, rome, sa, sb = seeded(r)
    recall_all(r, "Q3")
    first = context(r, "Q3").branch.bound_inputs["?person"]
    values = {"Alice": ("Paris", paris), "Bob": ("Rome", rome)}
    value, fid = values[first]
    ctx, response, receipt = decide(r, "Q3", (value, [fid]))
    assert receipt["branch_staged"] and not receipt["review_complete"]
    assert r.state.executions["Q3"].current_binding_id is None
    assert apply_memory(r, response, ctx) == receipt  # exact retry is idempotent
    restored = RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive, store=r.store, config=r.config)
    assert context(restored, "Q3").branch.bound_inputs["?person"] != first
    other = context(restored, "Q3").branch.bound_inputs["?person"]
    value, fid = values[other]
    decide(restored, "Q3", (value, [fid]))
    assert len(current(restored, "Q3").members) == 2
    assert replay_events(r.store.list_runtime_events(r.run_id)).export() == restored.export()


def test_equal_values_from_distinct_parents_keep_distinct_nodes_and_proofs():
    r = runtime()
    seeded(r)
    a, b = ingest(r, ("Q3", "Alice was born in Shared City."), ("Q3", "Bob was born in Shared City."))
    recall_all(r, "Q3")
    for _ in range(2):
        name = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Shared City", [a if name == "Alice" else b]))
    binding = current(r, "Q3")
    assert binding.value == "Shared City"  # convenience projection is not graph identity
    assert len(binding.members) == 2
    assert len({m.member_id for m in binding.members}) == 2
    assert len({tuple(m.parent_member_ids) for m in binding.members}) == 2


def test_noop_branch_does_not_hide_success_or_pretend_missing_is_empty():
    r = runtime()
    *_, paris, rome, sa, sb = seeded(r)
    recall_all(r, "Q3")
    for _ in range(2):
        person = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Paris", [paris])) if person == "Alice" else decide(r, "Q3", raw="NOOP")
    assert [m.value for m in current(r, "Q3").members] == ["Paris"]
    unresolved = evidence_pack(r).unresolved_or_conflicts
    assert any(d.get("state") == "UNRESOLVED_MEMBER_BRANCH" and d["bound_inputs"] == {"?person": "Bob"}
               for d in unresolved)


def test_add_member_rebinds_retires_descendants_and_correction_removes_old_edges():
    r = runtime()
    _, alice, bob, paris, rome, *_ = seeded(r)
    recall_all(r, "Q3")
    for _ in range(2):
        person = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Paris" if person == "Alice" else "Rome", [paris if person == "Alice" else rome]))
    old_q3 = current(r, "Q3").binding_id
    old_ids = {m.value: m.member_id for m in current(r, "Q2").members}
    charlie, = ingest(r, ("Q2", "Team Red includes Charlie."))
    decide(r, "Q2", ("Alice", [alice]), ("Bob", [bob]), ("Charlie", [charlie]))
    assert not r.state.binding_store[old_q3].valid
    assert r.state.executions["Q3"].current_binding_id is None
    assert {b.bound_inputs["?person"] for b in branches_for(r.state, "Q3")} == {"Alice", "Bob", "Charlie"}
    assert all(next(m for m in current(r, "Q2").members if m.value == name).member_id == mid
               for name, mid in old_ids.items())
    correction, = ingest(r, ("Q2", "Correction: Bob left Team Red; Charlie replaced him."))
    decide(r, "Q2", ("Alice", [alice]), ("Charlie", [charlie, correction]))
    graph = evidence_pack(r).navigation
    assert old_ids["Bob"] not in {m["member_id"] for m in graph["members"]}
    assert not any(old_ids["Bob"] in e.values() for e in graph["member_edges"])


@pytest.mark.parametrize("wire", [
    'BOUND | ["Alice", "Bob"] | {fid}', 'BOUND | null | {fid}',
    'BOUND | Alice | F_fake', 'BOUND | Alice | {fid}\nNOOP',
    'BOUND | Alice | {fid}\nBOUND | Bob | F_fake',
])
def test_invalid_member_response_never_partially_commits(wire):
    r = runtime()
    fid, = ingest(r, ("Q1", "Jane leads Team Red."))
    before = r.export()
    with pytest.raises(ValueError):
        decide(r, "Q1", raw=wire.format(fid=fid))
    assert r.export() == before


def test_single_named_entity_with_and_is_not_split_and_duplicate_proofs_merge():
    r = runtime()
    f1, f2 = ingest(r, ("Q1", "Jane leads Research and Development."), ("Q1", "Jane manages Research and Development."))
    decide(r, "Q1", ("Research and Development", [f1]), ("Research and Development", [f2]))
    assert len(current(r, "Q1").members) == 1
    assert set(current(r, "Q1").members[0].direct_fact_ids) == {f1, f2}


def test_prompts_no_fixed_cardinality_and_parser_preserves_legacy_only_for_old_plans():
    legacy = QueryPlanV3(plan_id="old", queries=[dict(id="Q1", template="Members?", output="?person")])
    assert member_plan(legacy).model_dump()["queries"][0].keys() == {"id", "template", "output", "inputs", "requires_complete_set"}
    r = runtime()
    ingest(r, ("Q1", "Jane leads Team Red."))
    ctx = context(r, "Q1")
    from delaybind_core.navigation_r2 import query_projection
    payload = dict(question="Test?", **ctx.model_dump(), query_instance=query_projection(r.state)["queries"][0], old_binding=None)
    text = messages("MEMORY", payload)[1]["content"]
    assert "cardinality=" not in text and "SINGLE" not in text
    assert MEMBER_MEMORY_PROMPT_VERSION in text and "one line per result" in text


def test_member_plan_prompt_matches_v51_request_and_retry():
    from delaybind_core.agent_prompts import EVIDENCE_ONLY_PLAN_VERSION, plan_prompt
    from delaybind_core.prompts_r2 import prompt_version_for

    payload = {"question": "Who directed Film Aspen?", "fact_only": True, "member_bindings": True}
    expected = plan_prompt(payload["question"], evidence_only=True)
    assert messages("PLAN", payload) == [{"role": "user", "content": expected}]
    assert prompt_version_for("PLAN", payload) == EVIDENCE_ONLY_PLAN_VERSION
    assert "Do not add intermediate reasoning or calculation queries." in expected
    assert "comparison, collect the relevant facts" in expected
    assert "For a final count, collect the members" in expected
    assert "depends_on must list exactly the producer query IDs" in expected
    assert "ordering-only dependencies" in expected
    assert "requires_complete_set" not in expected
    assert "Add an intermediate reasoning query only" in plan_prompt(payload["question"])
    assert "requires_complete_set: true" in plan_prompt(payload["question"])
    repair = messages("PLAN", {**payload, "validation_errors": ["bad dependency"]})
    assert repair[0]["content"] == (
        expected
        + "\nYour previous output was invalid: bad dependency"
          "\nReturn a corrected complete response in the specified format."
    )


def test_branch_budget_is_explicit_not_silent_truncation():
    r = runtime()
    seeded(r)
    with pytest.raises(MemberBranchLimit, match="MEMBER_BRANCH_BUDGET"):
        branches_for(r.state, "Q3", limit=1)


def test_scope_requirement_does_not_block_supported_members_until_eof():
    p = EvidencePlanR2(plan_id="all", queries=[dict(id="Q1", template="All members?", output="?person", requires_complete_set=True)])
    r = fixture(plan=p, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    f, = ingest(r, ("Q1", "The club includes Alice."))
    decide(r, "Q1", ("Alice", [f]))
    assert current(r, "Q1").members[0].value == "Alice"
    assert not evidence_pack(r).navigation["collection_scope_closed"]


def test_raw_review_mode_keeps_syntok_and_supports_multiple_member_lines():
    p = EvidencePlanR2(plan_id="raw", queries=[dict(id="Q1", template="Who teaches Cindy?", output="?teacher")])
    r = fixture(plan=p)
    from test_r2_core import ingest as raw_ingest
    a, b = raw_ingest(r, [("Q1", "D0:S0"), ("Q1", "D0:S3")])
    ctx = review_all(r, "Q1")
    response = parse_memory(f"BOUND | Alice | {a} | D0:S0\nBOUND | Bob | {b} | D0:S3", ctx)
    apply_memory(r, response, ctx)
    assert {m.value for m in current(r, "Q1").members} == {"Alice", "Bob"}
    assert current(r, "Q1").source_refs == ["D0:S0", "D0:S3"]
    assert_invariants(r.state)


def test_unrelated_raw_conflict_does_not_block_a_supported_member_but_remains_visible():
    p = EvidencePlanR2(plan_id="partial", queries=[dict(id="Q1", template="Who teaches Cindy?", output="?teacher")])
    r = fixture(plan=p)
    from test_r2_core import ingest as raw_ingest
    a, b = raw_ingest(r, [("Q1", "D0:S0"), ("Q1", "D0:S3")])
    ctx = review_all(r, "Q1", {b: "CONFLICT"})
    apply_memory(r, parse_memory(f"BOUND | Alice | {a} | D0:S0", ctx), ctx)
    assert current(r, "Q1").value == "Alice"
    assert any(d.get("fact_id") == b and d.get("status") == "CONFLICT"
               for d in evidence_pack(r).unresolved_or_conflicts)


def test_independent_parents_form_all_combinations_not_a_positional_zip():
    p = EvidencePlanR2(plan_id="independent", queries=[
        dict(id="Q1", template="Which cities?", output="?city"),
        dict(id="Q2", template="Which schools?", output="?school"),
        dict(id="Q3", template="Is ?school in ?city?", output="?match", inputs={"?city": "Q1", "?school": "Q2"})])
    r = fixture(plan=p, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    a, b, c, d = ingest(r, ("Q1", "Paris is a city."), ("Q1", "Rome is a city."),
                        ("Q2", "A is a school."), ("Q2", "B is a school."))
    decide(r, "Q1", ("Paris", [a]), ("Rome", [b]))
    decide(r, "Q2", ("A", [c]), ("B", [d]))
    assert {(x.bound_inputs["?city"], x.bound_inputs["?school"]) for x in branches_for(r.state, "Q3")} == {
        ("Paris", "A"), ("Paris", "B"), ("Rome", "A"), ("Rome", "B")}


def test_branch_noop_preserves_its_old_members_while_other_branch_changes():
    r = runtime()
    _, _, _, paris, rome, *_ = seeded(r)
    recall_all(r, "Q3")
    for _ in range(2):
        name = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Paris" if name == "Alice" else "Rome", [paris if name == "Alice" else rome]))
    bob_before = next(m for m in current(r, "Q3").members if m.value == "Rome")
    fixed, = ingest(r, ("Q3", "Correction: Alice was born in Lyon, not Paris."))
    for _ in range(2):
        name = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Lyon", [fixed])) if name == "Alice" else decide(r, "Q3", raw="NOOP")
    assert {m.value for m in current(r, "Q3").members} == {"Lyon", "Rome"}
    assert next(m for m in current(r, "Q3").members if m.value == "Rome") == bob_before


def test_all_noop_preserves_fact_uses_and_wakes_child_inbox_after_barrier():
    r = runtime()
    _, alice, bob, *_ = seeded(r)
    q2_id = current(r, "Q2").binding_id
    # One UPDATE touches an already-bound parent and its child. The parent's
    # review barrier must not permanently strand the child's new facts.
    ingest(r, ("Q1", "Unrelated account mentions Team Red."), ("Q2", "Team Red includes Charlie."))
    old_uses = {k: u.model_dump() for k, u in r.state.uses.items() if u.query_id == "Q1"}
    decide(r, "Q1", raw="NOOP")
    assert {k: u.model_dump() for k, u in r.state.uses.items() if u.query_id == "Q1"} == old_uses
    assert current(r, "Q2").binding_id == q2_id
    ctx = context(r, "Q2")
    assert ctx.allowed_mode == "REBIND"


def test_unresolved_branch_diagnostics_are_stable_across_sorted_json_resume():
    r = runtime()
    *_, paris, rome, sa, sb = seeded(r)
    recall_all(r, "Q3")
    for _ in range(2):
        name = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Paris", [paris])) if name == "Alice" else decide(r, "Q3", raw="NOOP")
    new, = ingest(r, ("Q3", "A new record confirms Bob was born in Rome."))
    for _ in range(2):
        name = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Rome", [new])) if name == "Bob" else decide(r, "Q3", raw="NOOP")
    restored = RuntimeR2(run_id=r.run_id, plan=r.state.plan, archive=r.archive, store=r.store, config=r.config)
    for rt in (r, restored):
        assert not any(d.get("query_id") == "Q3" and d.get("state") == "UNRESOLVED_MEMBER_BRANCH"
                       for d in evidence_pack(rt).unresolved_or_conflicts)


def test_stale_branch_after_parent_replacement_cannot_publish():
    r = runtime()
    _, a, b, paris, rome, *_ = seeded(r)
    recall_all(r, "Q3")
    stale = context(r, "Q3")
    response = parse_memory(f"BOUND | Paris | {paris}", stale)
    c, = ingest(r, ("Q2", "Team Red includes Charlie."))
    decide(r, "Q2", ("Alice", [a]), ("Bob", [b]), ("Charlie", [c]))
    before = r.export()
    with pytest.raises(ValueError, match="STALE"):
        apply_memory(r, response, stale)
    assert r.export() == before


def test_intermediate_branch_sql_failure_rolls_back_stage_and_receipt(monkeypatch):
    import sqlite3
    r = runtime()
    *_, paris, rome, sa, sb = seeded(r)
    recall_all(r, "Q3")
    ctx = context(r, "Q3")
    value, fid = ("Paris", paris) if ctx.branch.bound_inputs["?person"] == "Alice" else ("Rome", rome)
    response = parse_memory(f"BOUND | {value} | {fid}", ctx)
    before, database = r.export(), list(r.store.connection.iterdump())
    original, count = r.store._r2_execute, 0
    def fail(db, sql, params):
        nonlocal count
        count += 1
        if count == 5:
            raise sqlite3.OperationalError("member transaction failure")
        return original(db, sql, params)
    monkeypatch.setattr(r.store, "_r2_execute", fail)
    with pytest.raises(sqlite3.OperationalError):
        apply_memory(r, response, ctx)
    assert r.export() == before and list(r.store.connection.iterdump()) == database
    monkeypatch.setattr(r.store, "_r2_execute", original)
    assert apply_memory(r, response, ctx)["branch_staged"]


def test_plan_repair_preserves_cardinality_free_schema_and_rejects_fixed_cardinality():
    from delaybind_core.plan_repair_r2 import parse_repair
    raw = "REPAIR | F1\nUPSERT | Q3\nquery: Where did ?person live?\nEND UPSERT\nEND REPAIR"
    ops, _ = parse_repair(raw, plan())
    assert ops[0]["patch"]["template"] == "Where did ?person live?"
    with pytest.raises(ValueError):
        parse_repair(raw.replace("END UPSERT", "cardinality: SINGLE\nEND UPSERT"), plan())


def test_member_plan_round_trips_through_evaluation_validation():
    from delaybind_core.schema import parse_query_plan
    from types import SimpleNamespace
    from delaybind_core.plan_validation import validate_plan
    from delaybind_core.metrics import plan_relation_recall
    restored = parse_query_plan(plan().model_dump())
    assert restored == plan() and validate_plan(restored) == []
    assert plan_relation_recall(restored, SimpleNamespace(patterns=[])) is None


def test_full_text_runner_fans_out_and_answer_receives_graph_not_flattened_names():
    from delaybind_core.runner import V5Runner
    from delaybind_core.storage import SQLiteEventStore
    from delaybind_core.data import canonicalize_record
    from delaybind_core.smoke_r2 import read_request
    class Client:
        def __init__(self):
            self.branches = []
        async def complete(self, *, interface, messages, **kwargs):
            data = read_request(messages)
            if interface == "PLAN":
                return "Q1\nquery: Which team does Jane lead?\noutput: ?team\ndepends_on: NONE\n\nQ2\nquery: Who are the members of ?team?\noutput: ?person\ndepends_on: Q1\n\nQ3\nquery: Where was ?person born?\noutput: ?city\ndepends_on: Q2"
            if interface == "UPDATE":
                return "Q1 | Jane leads Team Red.\nQ2 | Team Red includes Alice.\nQ2 | Team Red includes Bob.\nQ3 | Alice was born in Paris.\nQ3 | Bob was born in Rome."
            if interface == "RECALL":
                if data["query_id"] == "Q2":
                    assert "Who are the members of Team Red?" in data["body"]
                    assert "Who are the members of ?team?" not in data["body"]
                if data["query_id"] == "Q3":
                    assert "Where was Alice born?" in data["body"]
                    assert "Where was Bob born?" in data["body"]
                    assert "Where was ?person born?" not in data["body"]
                return "SELECT " + ",".join(data["selectable"])
            if interface == "MEMORY":
                assert "cardinality=" not in messages[-1]["content"]
                by_text = {f["text"]: f["fact_id"] for f in data["facts"]}
                if data["query_id"] == "Q1":
                    return "BOUND | Team Red | " + by_text["Jane leads Team Red."]
                if data["query_id"] == "Q2":
                    return "\n".join("BOUND | " + p + " | " + by_text[f"Team Red includes {p}."] for p in ("Alice", "Bob"))
                q = data["rendered_query"]
                assert q in {"Where was Alice born?", "Where was Bob born?"}
                self.branches.append(q)
                person, city = ("Alice", "Paris") if "Alice" in q else ("Bob", "Rome")
                return f"BOUND | {city} | " + by_text[f"{person} was born in {city}."]
            if interface == "ANSWER":
                assert "Binding member nodes:" in data["body"] and " | parents=M" in data["body"]
                assert "source_member_id=" not in data["body"]
                assert " | facts=F" in data["body"]
                return r"\boxed{Paris and Rome}"
            raise AssertionError(interface)
    client = Client()
    sample = canonicalize_record(dict(id="fanout", question="Where were the members of Jane's team born?",
        context=[["Team", ["Jane leads Team Red. Team Red includes Alice and Bob. Alice was born in Paris. Bob was born in Rome."]]]))
    result = asyncio.run(V5Runner(client, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False)).run(
        run_id="fanout", sample=sample, store=SQLiteEventStore()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert set(client.branches) == {"Where was Alice born?", "Where was Bob born?"}
    assert len(result["state"]["member_graph"]["nodes"]) == 5
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1
