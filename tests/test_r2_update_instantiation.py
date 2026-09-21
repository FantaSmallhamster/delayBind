"""UPDATE targets follow effective member bindings without changing the plan.

These tests exercise the real runtime and wire rendering with scripted model
responses; they make no claims about extraction accuracy on a live API.
"""
import asyncio

from delaybind_core.context_r2 import build_update_context
from delaybind_core.member_graph_r2 import branches_for
from delaybind_core.navigation_r2 import query_projection, query_ready
from delaybind_core.prompts_r2 import messages
from delaybind_core.protocol_r2 import parse_update, parse_memory
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.schema_r2 import EvidencePlanR2
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.text_views_v52 import extraction_queries, extraction_targets_view, queries_view, update_routing_view
from test_r2_core import fixture, review_all, ingest as raw_ingest
from test_r2_member_graph import runtime, ingest, decide, seeded, current, context, recall_all


def snapshot(r):
    return build_update_context(r, "Test question?", ["D0:S0"])


def target(payload, qid):
    return next(q for q in payload["query_graph"]["queries"] if q["id"] == qid)


def finish_backgrounds(r, qid, mapping):
    recall_all(r, qid)
    for _ in range(2):
        person = context(r, qid).branch.bound_inputs["?person"]
        value, fid = mapping[person]
        decide(r, qid, (value, [fid]))


def test_single_binding_instantiates_update_but_never_rewrites_plan_or_recall():
    p = EvidencePlanR2(plan_id="film", queries=[
        dict(id="Q1", template="Who directed Film Cedar?", output="?director"),
        dict(id="Q2", template="What is the place of birth of ?director?", output="?place",
             inputs={"?director": "Q1"})])
    r = fixture(plan=p, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    before = snapshot(r)
    assert extraction_queries(target(before, "Q2")) == ["What is the place of birth of ?director?"]
    f, = ingest(r, ("Q1", "Film Cedar was directed by Otakar Vávra."))
    decide(r, "Q1", ("Otakar Vávra", [f]))
    state_before_projection = r.export()
    after = snapshot(r)
    q2 = target(after, "Q2")
    assert q2["rendered_query"] == "What is the place of birth of Otakar Vávra?"
    assert q2["bound_inputs"] == {"?director": "Otakar Vávra"}
    assert q2["extraction_instances"][0]["parent_member_ids"] == [current(r, "Q1").members[0].member_id]
    wire = messages("UPDATE", after)[1]["content"].split("Input data:\n", 1)[1]
    assert "Q2 | What is the place of birth of Otakar Vávra?" in wire
    assert "?director" not in wire
    assert "parent_member_ids" not in wire and "branch_id" not in wire
    assert r.export() == state_before_projection
    assert r.state.plan == p
    # The default graph/PLAN projection stays broad. UPDATE and RECALL opt into
    # the concrete member projection at their respective call sites.
    broad = query_projection(r.state)["queries"][1]
    assert broad["rendered_query"] == p.queries[1].template and broad["bound_inputs"] == {}


def test_fanout_displays_each_member_under_original_query_id():
    r = runtime()
    seeded(r)
    payload = snapshot(r)
    assert set(extraction_queries(target(payload, "Q3"))) == {"Where was Alice born?", "Where was Bob born?"}
    wire = extraction_targets_view(payload["query_graph"])
    assert wire.count("Q3 | ") == 2
    assert "Q3 | Where was ?person born?" not in wire
    assert "['Alice'" not in wire and "['Bob'" not in wire
    result, errors = parse_update("Q3 | Alice was born in Paris.\nQ3 | Bob was born in Rome.", payload)
    assert not errors and result is not None
    assert len(payload["query_graph"]["queries"]) == len(r.state.plan.queries)


def test_partial_inputs_are_visible_but_do_not_authorize_memory():
    r = runtime()
    _, _, _, paris, rome, _, _ = seeded(r)
    finish_backgrounds(r, "Q3", {"Alice": ("Paris", paris), "Bob": ("Rome", rome)})
    q5 = target(snapshot(r), "Q5")
    assert set(extraction_queries(q5)) == {"What connects Paris and ?school?", "What connects Rome and ?school?"}
    assert all(set(i["bound_inputs"]) == {"?city"} for i in q5["extraction_instances"])
    assert not query_ready(r.state, "Q5")
    assert branches_for(r.state, "Q5") == []  # MEMORY's readiness gate is unchanged.


def test_correlated_inputs_keep_lineages_and_never_cross_siblings():
    r = runtime()
    _, _, _, paris, rome, sa, sb = seeded(r)
    finish_backgrounds(r, "Q3", {"Alice": ("Paris", paris), "Bob": ("Rome", rome)})
    finish_backgrounds(r, "Q4", {"Alice": ("A College", sa), "Bob": ("B College", sb)})
    payload = snapshot(r)
    q5 = target(payload, "Q5")
    expected = {"What connects Paris and A College?", "What connects Rome and B College?"}
    assert set(extraction_queries(q5)) == expected
    for view in (extraction_targets_view, queries_view, update_routing_view):
        text = view(payload["query_graph"])
        assert all(t in text for t in expected)
        assert "What connects Paris and B College?" not in text
        assert "What connects Rome and A College?" not in text
        assert "What connects ?city and ?school?" not in text
    assert {i["branch_id"] for i in q5["extraction_instances"]} == {b.branch_id for b in branches_for(r.state, "Q5")}


def test_independent_inputs_form_all_combinations_instead_of_zipping():
    p = EvidencePlanR2(plan_id="independent", queries=[
        dict(id="Q1", template="Which cities?", output="?city"),
        dict(id="Q2", template="Which schools?", output="?school"),
        dict(id="Q3", template="Is ?school in ?city?", output="?match", inputs={"?city": "Q1", "?school": "Q2"})])
    r = fixture(plan=p, config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False))
    a, b, c, d = ingest(r, ("Q1", "Paris is a city."), ("Q1", "Rome is a city."),
                        ("Q2", "A is a school."), ("Q2", "B is a school."))
    decide(r, "Q1", ("Paris", [a]), ("Rome", [b]))
    decide(r, "Q2", ("A", [c]), ("B", [d]))
    assert set(extraction_queries(target(snapshot(r), "Q3"))) == {
        "Is A in Paris?", "Is A in Rome?", "Is B in Paris?", "Is B in Rome?"}


def test_no_compatible_join_does_not_fall_back_to_broad_template():
    r = runtime()
    _, _, _, paris, _, _, sb = seeded(r)
    for qid in ("Q3", "Q4"):
        recall_all(r, qid)
        for _ in range(2):
            person = context(r, qid).branch.bound_inputs["?person"]
            if qid == "Q3" and person == "Alice":
                decide(r, qid, ("Paris", [paris]))
            elif qid == "Q4" and person == "Bob":
                decide(r, qid, ("B College", [sb]))
            else:
                decide(r, qid, raw="NOOP")
    graph = snapshot(r)["query_graph"]
    assert extraction_queries(next(q for q in graph["queries"] if q["id"] == "Q5")) == []
    assert "Q5 |" not in extraction_targets_view(graph)
    assert "What connects" not in queries_view(graph)
    assert "Q5 [ACTIVE] evidence needed" not in update_routing_view(graph)


def test_staged_branch_results_are_not_published_as_update_inputs():
    r = runtime()
    _, _, _, paris, rome, _, _ = seeded(r)
    recall_all(r, "Q3")
    person = context(r, "Q3").branch.bound_inputs["?person"]
    decide(r, "Q3", ("Paris" if person == "Alice" else "Rome", [paris if person == "Alice" else rome]))
    assert r.state.executions["Q3"].current_binding_id is None
    assert extraction_queries(target(snapshot(r), "Q5")) == ["What connects ?city and ?school?"]


def test_rebind_updates_targets_but_repairs_keep_original_snapshot():
    r = runtime()
    _, alice, _, _, _, _, _ = seeded(r)
    frozen = snapshot(r)
    repair = {**frozen, "repair_targets": [], "rejected_items": [], "retained_items": [], "validation_errors": "error"}
    wire_before = messages("UPDATE_REPAIR", repair)
    correction, = ingest(r, ("Q2", "Correction: only Alice remains in Team Red."))
    # Pending re-review blocks the old result; it must not look authoritative.
    assert extraction_queries(target(snapshot(r), "Q3")) == ["Where was ?person born?"]
    decide(r, "Q2", ("Alice", [alice, correction]))
    assert extraction_queries(target(snapshot(r), "Q3")) == ["Where was Alice born?"]
    assert messages("UPDATE_REPAIR", repair) == wire_before
    assert "Q3 | Where was Bob born?" in wire_before[1]["content"]


def test_equal_rendered_values_deduplicate_text_not_member_proofs():
    r = runtime()
    seeded(r)
    a, b = ingest(r, ("Q3", "Alice was born in Shared City."), ("Q3", "Bob was born in Shared City."))
    finish_backgrounds(r, "Q3", {"Alice": ("Shared City", a), "Bob": ("Shared City", b)})
    q5 = target(snapshot(r), "Q5")
    assert len(q5["extraction_instances"]) == 2
    assert len({tuple(i["parent_member_ids"]) for i in q5["extraction_instances"]}) == 2
    assert extraction_queries(q5) == ["What connects Shared City and ?school?"]
    assert len(current(r, "Q3").members) == 2


def test_raw_source_mode_also_receives_concrete_targets():
    p = EvidencePlanR2(plan_id="raw", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Who is ?teacher's mother?", output="?mother", inputs={"?teacher": "Q1"})])
    r = fixture(plan=p)
    a, b = raw_ingest(r, [("Q1", "D0:S0"), ("Q1", "D0:S3")])
    ctx = review_all(r, "Q1")
    apply_memory(r, parse_memory(f"BOUND | Alice | {a} | D0:S0\nBOUND | Bob | {b} | D0:S3", ctx), ctx)
    wire = messages("UPDATE", snapshot(r))[1]["content"].split("Input data:\n", 1)[1]
    assert "Q2 [ACTIVE] Who is Alice's mother?" in wire
    assert "Q2 [ACTIVE] Who is Bob's mother?" in wire
    assert "evidence needed: Who is Alice's mother?" in wire
    assert "Who is ?teacher's mother?" not in wire
    assert "[D0:S0]" in wire  # Original evidence behavior is retained.


def test_legacy_nonmember_query_projection_is_unchanged():
    r = fixture()
    raw_ingest(r, [("Q1", "D0:S0")])
    ctx = review_all(r, "Q1")
    fid = ctx.allowed_fact_ids[0]
    apply_memory(r, parse_memory(f"BOUND | Alice | {fid} | D0:S0", ctx), ctx)
    assert query_projection(r.state, instantiate_members=True) == query_projection(r.state)
    assert target(snapshot(r), "Q2")["rendered_query"] == "Who is Alice's mother?"


def test_real_runner_refreshes_update_targets_between_windows():
    from delaybind_core.data import canonicalize_record, build_manifest
    from delaybind_core.smoke_r2 import read_request

    class Client:
        def __init__(self):
            self.updates = []

        async def complete(self, *, interface, messages, **kwargs):
            data = read_request(messages)
            if interface == "UPDATE":
                self.updates.append(data["body"])
                n = len(self.updates)
                if n == 1:
                    assert "Q2 | Who are the members of ?team?" in data["body"]
                    return "Q1 | Jane leads Team Red."
                if n == 2:
                    assert "Q2 | Who are the members of Team Red?" in data["body"]
                    assert "Q3 | Where was ?person born?" in data["body"]
                    return "Q2 | Team Red includes Alice.\nQ2 | Team Red includes Bob."
                assert n == 3
                assert "Q3 | Where was Alice born?" in data["body"]
                assert "Q3 | Where was Bob born?" in data["body"]
                assert "Q3 | Where was ?person born?" not in data["body"]
                return "Q3 | Alice was born in Paris.\nQ3 | Bob was born in Rome."
            if interface == "RECALL":
                return "SELECT " + ",".join(data["selectable"]) if data["selectable"] else "NONE"
            if interface == "MEMORY":
                facts = {f["text"]: f["fact_id"] for f in data["facts"]}
                if data["query_id"] == "Q1":
                    return "BOUND | Team Red | " + facts["Jane leads Team Red."]
                if data["query_id"] == "Q2":
                    if "Team Red includes Alice." not in facts:
                        return "NOOP"
                    return "\n".join("BOUND | " + p + " | " + facts[f"Team Red includes {p}."] for p in ("Alice", "Bob"))
                person, city = ("Alice", "Paris") if data["rendered_query"] == "Where was Alice born?" else ("Bob", "Rome")
                if f"{person} was born in {city}." not in facts:
                    return "NOOP"
                return f"BOUND | {city} | " + facts[f"{person} was born in {city}."]
            if interface == "ANSWER":
                assert "Alice was born in Paris." in data["body"] and "Bob was born in Rome." in data["body"]
                return r"\boxed{Paris and Rome}"
            raise AssertionError(interface)

    p = EvidencePlanR2(plan_id="stream", queries=[
        dict(id="Q1", template="Which team does Jane lead?", output="?team"),
        dict(id="Q2", template="Who are the members of ?team?", output="?person", inputs={"?team": "Q1"}),
        dict(id="Q3", template="Where was ?person born?", output="?city", inputs={"?person": "Q2"})])
    sample = canonicalize_record(dict(id="progressive", question="Where were the members of Jane's team born?",
        context=[["Team", ["Jane leads Team Red."]], ["Members", ["Team Red includes Alice and Bob."]],
                 ["Births", ["Alice was born in Paris. Bob was born in Rome."]]]))
    client = Client()
    result = asyncio.run(V5Runner(client, config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False, chunk_size=1)).run(
            run_id="progressive", sample=sample, manifest=build_manifest(sample), store=SQLiteEventStore(), plan=p))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert len(client.updates) == result["windows_processed"] == 3
    assert result["r2_metrics"]["transaction_replay_consistency"] == 1
