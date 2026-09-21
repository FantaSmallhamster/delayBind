"""Offline regression cases for bridge routing and bounded line repair."""

import asyncio

import pytest

from delaybind_core.agent_prompts_v52 import messages
from delaybind_core.evidence_context import build_update_context
from delaybind_core.fact_protocol_v52 import UpdateRepairScope, parse_update
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.v52_smoke import ScriptedSmokeClient, run_smoke
from test_memory_v52 import runtime_for, apply, assess, bind


def parse(raw):
    return parse_update(raw, query_ids=["Q1", "Q2"], visible_sources=["D0:S0", "D0:S1"])


@pytest.mark.parametrize("first,second,bridge,attribute,value,variable", [
    ("Who directed Example Film?", "Where was ?director born?",
     "Example Film was directed by Ada.", "Ada was born in Rome.", "Ada", "?director"),
    ("Who performs Example Song?", "Where was ?performer born?",
     "Example Song is performed by Bea.", "Bea was born in Lima.", "Bea", "?performer"),
    ("Who is Cora's husband?", "Who is ?husband's mother?",
     "Cora's husband is Dan.", "Dan's mother is Eva.", "Dan", "?husband"),
])
def test_bridge_checklist_and_forward_only_fact_routing(first, second, bridge, attribute, value, variable):
    plan = QueryPlanV3(plan_id="p", queries=[
        dict(id="Q1", template=first, output=variable),
        dict(id="Q2", template=second, output="?result", inputs={variable: "Q1"}),
    ])
    r = runtime_for(bridge + " " + attribute, plan=plan)
    prompt = messages("UPDATE", build_update_context(r, "two hop", ["D0:S0", "D0:S1"]))[-1]["content"]
    assert f"Bridge producer for Q2: evidence establishing {variable} belongs to Q1" in prompt
    assert "Q2 [DORMANT] evidence needed:" in prompt
    assert "把桥接关系和下游属性拆为各自完整的原子事实" in prompt
    assert bridge in prompt and attribute in prompt
    # Downstream-first output must still register Q1 as the bridge, without
    # reverse-binding it from Q2. This is scripted protocol coverage, not an LLM eval.
    update, errors = parse(f"Q2 | D0:S1 | {attribute} | DORMANT\nQ1 | D0:S0 | {bridge} | ACTIVE")
    assert not errors
    r.ingest_all(update, visible_sources=["D0:S0", "D0:S1"], window_index=0)
    assert r.state.executions["Q2"].status == "DORMANT"
    assert r.state.executions["Q1"].current_binding_id is None
    fid = next(f.fact_id for f in r.state.facts.values() if f.text == bridge)
    apply(r, "Q1", [assess("Q1", fid, "D0:S0"), bind("Q1", fid, value)])
    assert r.state.executions["Q2"].status != "DORMANT"
    assert any(j.query_id == "Q2" and j.stage == "SCAN" for j in r.state.recall_jobs.values())


@pytest.mark.parametrize("malformed", [
    "D0:S0 | Ada was born in Rome. | ACTIVE",  # Missing Q column.
    "Q99 | D0:S0 | Ada was born in Rome. | ACTIVE",
    "Q1 | D0:S999 | Ada was born in Rome. | ACTIVE",
    "Q1 | D0:S0 | Ada was born in Rome. | WRONG",
])
def test_repair_can_fix_metadata_without_changing_claim(malformed):
    _, rejected = parse(malformed)
    scope = UpdateRepairScope(rejected)
    repaired, errors = parse("Q2 | D0:S0,D0:S1 | Ada was born in Rome. | DORMANT")
    assert not errors
    accepted, errors = scope.restrict(repaired)
    assert not errors and len(accepted.facts) == 1
    assert scope.targets == []
    assert scope.restrict(repaired)[1][0]["error"] == "UPDATE_REPAIR_OUT_OF_SCOPE"


def test_repair_blocks_unrelated_rows_rewrites_and_type_changes_across_retries():
    _, rejected = parse("Q99 | D0:S0 | Ada was born in Rome. | ACTIVE")
    scope = UpdateRepairScope(rejected)
    for raw in [
        "Q2 | D0:S1 | Rain Or Shine is an album. | ACTIVE",
        "Q1 | D0:S0 | Ada was born in Paris. | ACTIVE",
        "PLAN_HINT | D0:S0 | Ada was born in Rome.",
    ]:
        candidate, errors = parse(raw)
        assert not errors
        accepted, errors = scope.restrict(candidate)
        assert not accepted.facts and not accepted.hints
        assert errors[0]["error"] == "UPDATE_REPAIR_OUT_OF_SCOPE"
        assert len(scope.targets) == 1  # A failed repair cannot become a target.
    accepted, errors = scope.restrict(parse("Q1 | D0:S0 | Ada was born in Rome. | ACTIVE")[0])
    assert len(accepted.facts) == 1 and not errors


def test_repair_hint_and_escaped_claim_keep_their_original_identity():
    _, rejected = parse(r"PLAN_HINT | D0:S999 | Missing A \| B.\nNeed context.")
    scope = UpdateRepairScope(rejected)
    accepted, errors = scope.restrict(parse(r"PLAN_HINT | D0:S0 | Missing A \| B.\nNeed context.")[0])
    assert not errors and len(accepted.hints) == 1
    assert accepted.hints[0].text == "Missing A | B.\nNeed context."


def test_repair_malformed_unrecoverable_item_grants_no_extraction_scope():
    _, rejected = parse("unstructured invalid response")
    scope = UpdateRepairScope(rejected)
    assert scope.targets == []
    accepted, errors = scope.restrict(parse("Q1 | D0:S0 | new fact | ACTIVE")[0])
    assert not accepted.facts and errors
    assert scope.restrict(parse("NONE")[0])[1] == []


@pytest.mark.parametrize("keep_drifting", [False, True])
def test_runner_preserves_valid_rows_and_never_ingests_repair_drift(keep_drifting):
    class Client(ScriptedSmokeClient):
        repair_calls = 0

        async def complete(self, *, interface, messages, **kwargs):
            if interface == "UPDATE":
                original = await super().complete(interface=interface, messages=messages, **kwargs)
                self.original_lines = original.splitlines()
                # Keep bridge Q1 valid; reject both downstream references.
                return "\n".join(line if line.startswith("Q1 ") else line.replace(line.split(" | ")[1], "D0:S999")
                                 for line in self.original_lines)
            if interface == "UPDATE_REPAIR":
                self.repair_calls += 1
                prompt = messages[-1]["content"]
                targets = prompt.split("Allowed repair targets:\n")[1].split("\n\n", 1)[0]
                assert "Cindy's teacher is Alice." not in targets
                if self.repair_calls == 1:
                    return (next(line for line in self.original_lines if line.startswith("Q2 "))
                            + "\nQ2 | D0:S0 | An unrelated album exists. | ACTIVE")
                assert "Alice's mother is Mary." not in targets  # Consumed exactly once.
                assert "unrelated album" not in targets
                assert "UPDATE_REPAIR_OUT_OF_SCOPE" in prompt
                if keep_drifting:
                    return "Q2 | D0:S0 | An unrelated album exists. | ACTIVE"
                return next(line for line in self.original_lines if line.startswith("Q3 "))
            return await super().complete(interface=interface, messages=messages, **kwargs)

    client = Client()
    result = asyncio.run(run_smoke(client=client, config=RunnerConfig(
        protocol_version="v5.2", chunk_size=1000, max_protocol_retries=2)))
    assert client.repair_calls == 2
    texts = {f["text"] for f in result["state"]["facts"].values()}
    assert "Cindy's teacher is Alice." in texts
    assert "Alice's mother is Mary." in texts
    assert not any("album" in t for t in texts)
    if keep_drifting:
        assert result["status"] == "RUNTIME_ERROR"
        assert "UPDATE_REPAIR_EXHAUSTED" in result["reason_codes"]
    else:
        assert result["status"] == "ANSWERED", result["reason_codes"]
        assert result["answer"]["answer"] == "Suzhou"
        assert result["interface_calls"]["RECALL"] == 2
