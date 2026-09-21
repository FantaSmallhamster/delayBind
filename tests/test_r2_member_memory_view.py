"""Lossless evidence/graph presentation, without changing binding authority."""

from copy import deepcopy
from dataclasses import replace
import re

from delaybind_core.context_r2 import budgeted_evidence_pack, evidence_pack
from delaybind_core.prompts_r2 import messages
from delaybind_core.text_views_v52 import escaped, memory_view
from delaybind_core.token_budget import TokenCounter
from test_r2_core import context, recall_all
from test_r2_member_graph import runtime, seeded, ingest, decide
from test_v52_sources import CharacterTokenizer


def render(pack):
    return memory_view(pack, include_raw=False, include_sources=False)


def populated():
    r = runtime()
    seeded(r)
    a, b = ingest(r, ("Q3", "Alice was born in Shared City."),
                  ("Q3", "Bob was born in Shared City."))
    recall_all(r, "Q3")
    for _ in range(2):
        name = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Shared City", [a if name == "Alice" else b]))
    return r


def test_compact_view_keeps_all_facts_and_is_read_only_and_deterministic():
    r = populated()
    state_before = r.export()
    pack = evidence_pack(r).model_dump(mode="json")
    # A second use must not duplicate the fact text or lose its distinct status.
    fact = pack["navigation"]["facts"][0]
    pack["navigation"]["facts"].append({**fact, "query_id": "Q99", "use_status": "CANDIDATE"})
    before = deepcopy(pack)
    text = render(pack)
    for f in pack["navigation"]["facts"]:
        assert text.count(escaped(f["text"])) == 1
        assert f["fact_id"] not in text
        assert f["input_signature"] not in text
    assert "Q99:CANDIDATE" in text
    assert "Evidence links:" not in text and "Member dependency and support edges:" not in text
    assert "input_signature" not in text and "branch_id=" not in text
    assert pack == before and r.export() == state_before
    for key in ("facts", "members", "member_edges", "links", "binding_ports"):
        pack["navigation"][key].reverse()
    assert render(pack) == text


def test_member_lines_encode_every_support_and_parent_edge_without_merging_values():
    r = populated()
    pack = evidence_pack(r).model_dump(mode="json")
    nav = pack["navigation"]
    text = render(pack)
    facts = {f"F{i}": fid for i, fid in enumerate(sorted({f["fact_id"] for f in nav["facts"]}), 1)}
    members = {f"M{i}": mid for i, mid in enumerate(sorted(m["member_id"] for m in nav["members"]), 1)}
    edges = set()
    values = []
    for line in text.splitlines():
        match = re.fullmatch(r"(M\d+) \| (Q\d+) (\?\w+) = (.*?) \| facts=(.*?) \| parents=(.*)", line)
        if not match:
            continue
        mid = members[match[1]]
        values.append(match[4])
        for fid in match[5].split(","):
            if fid != "NONE":
                edges.add(("FACT_SUPPORT", facts[fid], mid))
        for parent in match[6].split(","):
            if parent != "NONE":
                edges.add(("MEMBER_DEPENDENCY", members[parent], mid))
    expected = {(e["kind"], e.get("source_fact_id", e.get("source_member_id")), e["target_member_id"])
                for e in nav["member_edges"]}
    assert edges == expected
    assert len(values) == len(nav["members"])
    assert values.count("Shared City") == 2
    # Unresolved consumers still retain their query-level routing, without
    # assigning every upstream member to every downstream fact.
    assert "Q2 ?person -> Q3,Q4" in text


def test_unresolved_branch_and_open_collection_remain_explicit():
    r = runtime()
    *_, paris, rome, sa, sb = seeded(r)
    recall_all(r, "Q3")
    for _ in range(2):
        person = context(r, "Q3").branch.bound_inputs["?person"]
        decide(r, "Q3", ("Paris", [paris])) if person == "Alice" else decide(r, "Q3", raw="NOOP")
    pack = evidence_pack(r).model_dump(mode="json")
    text = render(pack)
    assert "UNRESOLVED_MEMBER_BRANCH" in text and "?person=Bob" in text
    assert "effective=false" in text and "branch_id=BR1" in text
    assert "Collection scope closed:\nfalse" in text
    for d in pack["unresolved_or_conflicts"]:
        if "branch_id" in d:
            assert d["branch_id"] not in text
    r.close_scope()
    closed = render(evidence_pack(r).model_dump(mode="json"))
    assert "Collection scope closed:\ntrue" in closed
    assert "UNRESOLVED_MEMBER_BRANCH" in closed


def test_identifiers_inside_fact_text_and_values_are_not_rewritten():
    r = populated()
    pack = evidence_pack(r).model_dump(mode="json")
    mid = pack["navigation"]["members"][0]["member_id"]
    literal = f"Literal identifier {mid} | preserve this\nsecond line."
    pack["navigation"]["facts"][0]["text"] = literal
    pack["navigation"]["members"][0]["value"] = literal
    text = render(pack)
    assert text.count(escaped(literal)) == 2


def test_missing_overlay_text_is_not_invented_and_diagnostics_keep_short_fact_ids():
    r = populated()
    pack = evidence_pack(r).model_dump(mode="json")
    missing = pack["navigation"]["facts"].pop()
    pack["unresolved_or_conflicts"].append(dict(query_id="Q3", fact_id=missing["fact_id"],
        status="CONFLICT", effective=False, input_signature=missing["input_signature"]))
    text = render(pack)
    assert "Referenced fact text not shown in this view:" in text
    assert missing["text"] not in text and missing["fact_id"] not in text
    assert "status=CONFLICT" in text and "fact_id=F" in text


def test_answer_and_budget_use_identical_compact_view_without_evicting_facts():
    r = populated()
    before = r.export()
    pack = evidence_pack(r).model_dump(mode="json")
    text = render(pack)
    counter = TokenCounter(CharacterTokenizer())
    # The old metadata-heavy view would fail this same limit.
    old = memory_view(pack, include_raw=True, include_sources=False)
    assert len(old) > len(text) * 3
    r.config = replace(r.config, memory_token_budget=counter.count(text))
    budgeted = budgeted_evidence_pack(r, counter).model_dump(mode="json")
    assert budgeted == pack and r.export() == before
    prompt = messages("ANSWER", dict(question="Where?", working_memory=budgeted,
        fact_only=True, member_bindings=True, answer_contract="boxed"))[-1]["content"]
    assert "Working memory:\n\n" + text in prompt
    assert "view_hidden_fact_ids" not in budgeted["navigation"]


def test_raw_and_legacy_views_keep_durable_ids_and_source_protocol():
    r = populated()
    pack = evidence_pack(r).model_dump(mode="json")
    fid = pack["navigation"]["facts"][0]["fact_id"]
    raw = memory_view(pack)
    assert fid in raw and "Evidence links:" in raw and "Original evidence:" in raw
    del pack["navigation"]["members"]
    legacy = render(pack)
    assert fid in legacy and "Evidence links:" in legacy


def test_empty_graph_and_diagnostics_are_not_silently_dropped():
    pack = dict(navigation=dict(facts=[], members=[], binding_ports=[], collection_scope_closed=False),
                unresolved_or_conflicts=[dict(query_id="Q1", status="ACTIVE", effective=False)])
    text = render(pack)
    assert "Fact navigation:\nNONE" in text and "status=ACTIVE" in text
    assert "Collection scope closed:\nfalse" in text
