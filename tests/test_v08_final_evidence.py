"""V08 final evidence packs and persistent source assessments."""

import asyncio
import json

from delaybind_core.archive import RawArchive
from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.fact_protocol import BindingProposal, SourceCheck, parse_update
from delaybind_core.manifest import ManifestEntry
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subqueries import SubqueryRuntime
from delaybind_core.subquery_runner import run_subqueries
from delaybind_core.working_memory import FactNode


def entry(ref, text, position):
    return ManifestEntry(source_ref=ref, sample_id="v08", dataset_id="test",
                         document_id=ref, title=ref, sentence_id=0,
                         stream_position=position, text=text)


def single_runtime():
    store = SQLiteEventStore()
    archive = RawArchive(store, "v08")
    archive.append([entry("s1", "Ada directed Fern in 1978.", 0)])
    plan = QueryPlan(plan_id="v08", queries=[
        Subquery(id="Q1", template="Who directed Fern?", output="?director"),
    ])
    return store, SubqueryRuntime(run_id="v08", plan=plan, archive=archive, store=store)


def test_source_diff_keeps_fact_immutable_and_shows_raw_source_for_terminal_result():
    store, runtime = single_runtime()
    try:
        saved = "Bea directed Fern in 1986."
        runtime.ingest(parse_update(f"Q1 | s1 | {saved}"), window_index=0, allowed_refs={"s1"})
        fact_id = next(iter(runtime.state.accepted_ids("Q1")))
        original = runtime.state.facts[fact_id].model_copy(deep=True)
        runtime.record_source_assessments([
            SourceCheck(query_id="Q1", fact_id=fact_id, verdict="SOURCE_DIFF",
                        source_refs=["s1"], note="The saved year and person differ."),
        ], scope_keys={"Q1": "scope-v1"}, window_index=0)
        runtime.apply_binding(BindingProposal(query_id="Q1", value="Ada", support_refs=[fact_id]),
                              window_index=0)

        pack = runtime.final_evidence_pack()
        assert pack["results"][0]["query_id"] == "Q1"  # terminal queries are retained
        assert pack["results"][0]["value"] == "Ada"
        assert pack["confirmed_facts"][0]["fidelity"] == "SOURCE_DIFF"
        assert pack["raw_sources"][0]["text"] == "Ada directed Fern in 1978."
        assert runtime.state.facts[fact_id] == original
        assert fact_id == FactNode.create(saved, ["s1"], 99).fact_id
        rendered = runtime.render_final_evidence_pack(pack)
        assert "SAVED_WORDING_INACCURATE_USE_RAW_SOURCE" in rendered
        assert "The saved year and person differ" not in rendered  # diagnostic note is not evidence
        link = next(iter(runtime.state.links.values()))
        assert link.source_versions["s1"]
    finally:
        store.close()


def test_deferred_fact_is_excluded_then_admitted_unbound_fact_is_supplemental_only():
    store = SQLiteEventStore()
    try:
        archive = RawArchive(store, "v08-route")
        archive.append([entry("s1", "Ada directed Fern.", 0),
                        entry("s2", "Ada's father was Charles.", 1)])
        plan = QueryPlan(plan_id="v08-route", queries=[
            Subquery(id="Q1", template="Who directed Fern?", output="?director"),
            Subquery(id="Q2", template="Where was ?director born?", output="?birthplace", depends_on=["Q1"]),
        ])
        runtime = SubqueryRuntime(run_id="v08-route", plan=plan, archive=archive, store=store)
        runtime.ingest(parse_update(
            "Q1 | s1 | Ada directed Fern.\nQ2 | s2 | Ada's father was Charles."
        ), window_index=0, allowed_refs={"s1", "s2"})
        candidate = runtime.candidate_facts("Q2")[0]
        assert candidate.fact_id not in {fact["fact_id"] for fact in runtime.final_evidence_pack()["facts"]}

        parent = next(iter(runtime.state.accepted_ids("Q1")))
        runtime.apply_binding(BindingProposal(query_id="Q1", value="Ada", support_refs=[parent]),
                              window_index=0)
        execution = runtime.state.executions["Q2"]
        runtime.admit_recalled("Q2", {candidate.fact_id}, expected_query_version=execution.version,
                                expected_binding_version=execution.binding_version)
        runtime.finish_review("Q2")
        pack = runtime.final_evidence_pack()
        supplemental = {fact["fact_id"]: fact for fact in pack["supplemental_context"]}
        assert supplemental[candidate.fact_id]["proof_status"] == "SUPPLEMENTAL_NOT_BOUND"
        assert not any(result["query_id"] == "Q2" for result in pack["results"])
        assert "SUPPLEMENTAL CONTEXT (NOT BINDING PROOF)" in runtime.render_final_evidence_pack(pack)
    finally:
        store.close()


def test_source_backed_verdict_without_sources_is_not_persisted():
    store, runtime = single_runtime()
    try:
        runtime.ingest(parse_update("Q1 | s1 | Ada directed Fern."), window_index=0, allowed_refs={"s1"})
        fact_id = next(iter(runtime.state.accepted_ids("Q1")))
        runtime.record_source_assessments([
            SourceCheck(query_id="Q1", fact_id=fact_id, verdict="SUPPORTED", source_refs=[], note="claim"),
        ], scope_keys={"Q1": "scope"}, window_index=0)
        assert not runtime.state.source_assessments
    finally:
        store.close()


class BudgetClient:
    def __init__(self, store):
        self.store = store
        self.calls = []

    async def complete(self, *, run_id, interface, messages, **kwargs):
        self.calls.append(interface)
        prompt = messages[0]["content"]
        if interface == "UPDATE":
            payload = json.loads(self.store.connection.execute(
                "select payload_json from raw_archive where run_id=?", (run_id,)
            ).fetchone()[0])
            return f"Q1 | {payload['source_ref']} | Ada directed Fern."
        if interface == "MEMORY_GROUNDED":
            rows = json.loads(prompt.split(
                "Facts to check (use allowed_context_refs for citations):\n", 1
            )[1].split("\n\nOriginal source excerpts:", 1)[0])
            row = rows[0]
            return (f"CHECKS\nQ1 | {row['fact_id']} | SUPPORTED | "
                    f"{row['allowed_target_refs'][0]} | faithful\n"
                    f"BINDINGS\nBIND | Q1 | Ada | {row['fact_id']}")
        if interface == "ANSWER":
            raise AssertionError("ANSWER must not run after a final-request budget failure")
        raise AssertionError(interface)


def test_complete_answer_request_budget_failure_is_observable():
    store = SQLiteEventStore()
    try:
        client = BudgetClient(store)
        sample = CanonicalSample(sample_id="budget", question="Who directed Fern?", documents=[
            CanonicalDocument(document_id="d1", title="Fern", sentences=["Ada directed Fern."]),
        ])
        plan = QueryPlan(plan_id="budget", queries=[
            Subquery(id="Q1", template="Who directed Fern?", output="?director"),
        ])
        result = asyncio.run(run_subqueries(
            client=client,
            config=RunnerConfig(answer_format="boxed", snapshot_every_windows=0,
                                memory_source_mode="joint_source_memory", memory_char_budget=1000),
            run_id="budget", sample=sample, store=store, plan=plan,
        ))
        assert result["state"]["status"] == "RESOURCE_LIMIT"
        assert "ANSWER" not in client.calls
        events = store.list_runtime_events("budget")
        failure = next(event for event in events if event.event_type == "FINAL_EVIDENCE_BUDGET_EXCEEDED")
        assert failure.payload["size"] > failure.payload["limit"] == 1000
    finally:
        store.close()
