import asyncio

import pytest

from delaybind_core.agent_prompts_v52 import messages
from delaybind_core.evidence_context import build_update_context, build_memory_context
from delaybind_core.text_protocol_v52 import parse_memory
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.v52_smoke import ScriptedSmokeClient, read_fixture_request
from test_memory_v52 import runtime_for, ingest, apply, assess, bind


def test_update_includes_accepted_unbound_memory_but_not_dormant_candidates():
    r = runtime_for()
    active = ingest(r, ["Q1"], "D0:S0")
    latent = ingest(r, ["Q3"], "D0:S2")
    apply(r, "Q1", [assess("Q1", active, "D0:S0")])
    payload = build_update_context(r, "q", ["D0:S1"])
    facts = payload["working_memory"]["navigation"]["facts"]
    assert [f["fact_id"] for f in facts] == [active]
    assert latent not in {f["fact_id"] for f in facts}
    assert payload["visible_sources"] == ["D0:S0", "D0:S1"]
    assert [s["source_ref"] for s in payload["working_memory"]["raw_evidence"]] == ["D0:S0"]
    prompt = messages("UPDATE", payload)[-1]["content"]
    assert "Working memory:\nFact navigation:\n" + active in prompt
    assert "Cindy's teacher is Alice." in prompt
    assert "Mary was born in Suzhou." not in prompt
    assert "text_sha256" not in prompt


def test_update_pins_binding_proof_and_deduplicates_current_window():
    r = runtime_for()
    fid = ingest(r, ["Q1"], "D0:S0")
    apply(r, "Q1", [assess("Q1", fid, "D0:S0"), bind("Q1", fid), {"op": "FOCUS", "fact_ids": []}])
    payload = build_update_context(r, "q", ["D0:S1"])
    assert payload["working_memory"]["navigation"]["facts"][0]["fact_id"] == fid
    assert "D0:S0" in payload["visible_sources"]
    duplicate = build_update_context(r, "q", ["D0:S0"])
    assert duplicate["working_memory"]["raw_evidence"] == []
    prompt = messages("UPDATE", duplicate)[-1]["content"]
    assert prompt.count("[D0:S0] ") == 1


def test_cross_window_reader_can_cite_memory_raw_and_repair_same_sources():
    class Client(ScriptedSmokeClient):
        saw_history = False
        repair_sources = None

        async def complete(self, *, interface, messages, **kwargs):
            data = read_fixture_request(messages)
            if interface in {"UPDATE", "UPDATE_REPAIR"}:
                current = [s["source_ref"] for s in data["window_sources"]]
                if "D0:S1" in current:
                    assert data["working_memory"]["raw_evidence"][0]["source_ref"] == "D0:S0"
                    assert data["visible_sources"] == ["D0:S0", "D0:S1"]
                    self.saw_history = True
                    if interface == "UPDATE":
                        self.repair_sources = data["visible_sources"]
                        return "Q1 | D0:S999 | Mary was born in Suzhou. | ACTIVE"
                    assert data["visible_sources"] == self.repair_sources
                    return "Q1 | D0:S0,D0:S1 | Mary was born in Suzhou. | ACTIVE"
                return "Q1 | D0:S0 | Mary's name is Mary. | ACTIVE"
            return await super().complete(interface=interface, messages=messages, **kwargs)

    client, store = Client(), SQLiteEventStore()
    plan = QueryPlanV3(plan_id="p", queries=[{"id": "Q1", "template": "Where was Mary born?", "output": "?city"}])
    result = asyncio.run(V5Runner(client, config=RunnerConfig(protocol_version="v5.2", chunk_size=18)).run(
        run_id="reader", question="Where was Mary born?", context="Mary's name is Mary. She was born in Suzhou.",
        store=store, plan=plan))
    assert client.saw_history
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert result["interface_calls"]["UPDATE_REPAIR"] == 1
    calls = [m for m in result["context_manifests"] if m.get("interface") in {"UPDATE", "UPDATE_REPAIR"}]
    assert calls[-1]["visible_source_refs"] == calls[-2]["visible_source_refs"]
    assert set(calls[-1]["visible_source_refs"]) == {"D0:S0", "D0:S1"}
    assert any(set(f["source_refs"]) == {"D0:S0", "D0:S1"} for f in result["state"]["facts"].values())


def test_patch_text_block_derives_inputs_and_preserves_atomic_route():
    r = runtime_for()
    fid = ingest(r, ["Q1"], "D0:S0")
    ctx = build_memory_context(r, "Q1")
    raw = (f"ASSESS | Q1 | {fid} | ACCEPT | D0:S0 | RAW_SUPPORTED\n"
           f"PATCH | Q4 | {fid}\nquery: Which person teaches Cindy?\noutput: ?person\ndepends_on: NONE\nEND PATCH\n"
           f"ROUTE | Q4 | {fid}")
    proposal = parse_memory(raw, ctx.context_id, plan=r.state.plan)
    r.apply_memory(proposal, ctx)
    assert next(q for q in r.state.plan.queries if q.id == "Q4").inputs == {}
    assert any(j.query_id == "Q4" and j.stage == "SCAN" for j in r.state.recall_jobs.values())
    prior = r.export()
    bad = f"PATCH | Q5 | {fid}\nquery: Where was ?missing born?\noutput: ?place\ndepends_on: Q1\nEND PATCH"
    with pytest.raises(ValueError, match="PATCH_UNKNOWN_VARIABLE"):
        parse_memory(bad, ctx.context_id, plan=r.state.plan)
    assert r.export() == prior


@pytest.mark.parametrize("body", [
    'PATCH | Q1 | {"template":"new"}',
    "PATCH | Q1 | NONE\nquery: new",
    "PATCH | Q1 | NONE\nquery: first\nquery: second\nEND PATCH",
    "PATCH | Q2 | NONE\nquery: Where was ?teacher born?\ndepends_on: NONE\nEND PATCH",
])
def test_patch_rejects_json_unclosed_duplicate_or_inconsistent_fields(body):
    with pytest.raises(ValueError):
        parse_memory(body, "c", plan=runtime_for().state.plan)


def test_patch_existing_query_keeps_unmodified_fields_and_accepts_forward_producers():
    r = runtime_for()
    proposal = parse_memory("PATCH | Q2 | NONE\nquery: Where was ?teacher born?\ndepends_on: Q1\nEND PATCH", "c", plan=r.state.plan)
    patch = proposal.operations[0].patch.model_dump(exclude_unset=True)
    assert patch == {"template": "Where was ?teacher born?", "inputs": {"?teacher": "Q1"}}
    raw = ("PATCH | Q4 | NONE\nquery: Where was ?new born?\noutput: ?new_city\ndepends_on: Q5\nEND PATCH\n"
           "PATCH | Q5 | NONE\nquery: Who?\noutput: ?new\ndepends_on: NONE\nEND PATCH")
    assert parse_memory(raw, "c", plan=r.state.plan).operations[0].patch.inputs == {"?new": "Q5"}
