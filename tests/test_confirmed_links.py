"""V07 keeps candidate routing separate from confirmed binding evidence."""

from delaybind_core.archive import RawArchive
from delaybind_core.fact_protocol import BindingProposal, parse_update
from delaybind_core.manifest import ManifestEntry
from delaybind_core.schema import QueryPlan, RuntimeEvent, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subqueries import SubqueryRuntime, replay_subqueries


def plan():
    return QueryPlan(plan_id="v07", queries=[
        Subquery(id="Q1", template="Who directed Fern?", output="?director"),
        Subquery(id="Q2", template="Where was ?director born?", output="?birthplace", depends_on=["Q1"]),
    ])


def entry(ref, text, position):
    return ManifestEntry(source_ref=ref, sample_id="v07", dataset_id="test",
                         document_id=ref, title=ref, sentence_id=0,
                         stream_position=position, text=text)


def runtime():
    store = SQLiteEventStore()
    archive = RawArchive(store, "v07")
    archive.append([
        entry("s1", "Ada directed Fern.", 0),
        entry("s2", "Cochin was born in 1924.", 1),
        entry("s3", "Ada was born in London.", 2),
        entry("s4", "Bea directed Fern.", 3),
    ])
    return store, SubqueryRuntime(run_id="v07", plan=plan(), archive=archive, store=store)


def ingest(rt, raw, window=0):
    return rt.ingest(parse_update(raw), window_index=window,
                     allowed_refs={"s1", "s2", "s3", "s4"})


def bind_parent_and_admit_child(rt, child_text="Cochin was born in 1924.", child_ref="s2"):
    ingest(rt, f"Q1 | s1 | Ada directed Fern.\nQ2 | {child_ref} | {child_text}")
    parent_id = next(iter(rt.state.accepted_ids("Q1")))
    rt.apply_binding(BindingProposal(query_id="Q1", value="Ada", support_refs=[parent_id]),
                     window_index=0)
    execution = rt.state.executions["Q2"]
    candidate = rt.candidate_facts("Q2")[0]
    rt.admit_recalled("Q2", {candidate.fact_id}, expected_query_version=execution.version,
                       expected_binding_version=execution.binding_version)
    rt.finish_review("Q2")
    return parent_id, candidate.fact_id


def test_ingest_and_recall_do_not_publish_candidate_relationships():
    store, rt = runtime()
    try:
        parent_id, child_id = bind_parent_and_admit_child(rt)
        assert len(rt.state.links) == 1
        parent_link = next(iter(rt.state.links.values()))
        assert parent_link.kind == "CONFIRMED_BINDING"
        assert parent_link.query_id == "Q1"
        assert parent_link.support_fact_ids == [parent_id]
        assert not any(link.query_id == "Q2" for link in rt.state.links.values())
        rendered = rt.memory_text(visible_only=False)
        assert "Cochin was born in 1924" in rendered
        assert "CONFIRMED Q2" not in rendered
        assert child_id in rt.state.accepted_ids("Q2")
    finally:
        store.close()


def test_successful_binding_records_rendered_query_inputs_support_and_version():
    store, rt = runtime()
    try:
        parent_id, child_id = bind_parent_and_admit_child(
            rt, child_text="Ada was born in London.", child_ref="s3")
        rt.apply_binding(BindingProposal(query_id="Q2", value="London", support_refs=[child_id]),
                         window_index=1)
        link = next(link for link in rt.state.links.values() if link.query_id == "Q2")
        execution = rt.state.executions["Q2"]
        assert link.kind == "CONFIRMED_BINDING"
        assert link.rendered_query == "Where was Ada born?"
        assert link.value == "London"
        assert link.effective_upstream_bindings == {"?director": "Ada"}
        assert link.support_fact_ids == sorted([parent_id, child_id])
        assert link.source_refs == ["s1", "s3"]
        assert (link.query_version, link.binding_version, link.resolved_version) == (
            execution.version, execution.binding_version, execution.resolved_version)
        assert "CONFIRMED Q2" in rt.memory_text(visible_only=False)
    finally:
        store.close()


def test_failed_binding_does_not_create_relationship():
    store, rt = runtime()
    try:
        ingest(rt, "Q1 | s1 | Ada directed Fern.")
        try:
            rt.apply_binding(BindingProposal(query_id="Q1", value="Ada", support_refs=["Fmissing"]),
                             window_index=0)
        except ValueError:
            pass
        assert not rt.state.links
    finally:
        store.close()


def test_value_change_invalidates_downstream_but_preserves_fact_nodes():
    store, rt = runtime()
    try:
        _, child_id = bind_parent_and_admit_child(
            rt, child_text="Ada was born in London.", child_ref="s3")
        rt.apply_binding(BindingProposal(query_id="Q2", value="London", support_refs=[child_id]),
                         window_index=1)
        ingest(rt, "Q1 | s4 | Bea directed Fern.", window=2)
        replacement = next(fid for fid in rt.state.accepted_ids("Q1") if fid != rt.state.executions["Q1"].support_fact_ids[0])
        rt.apply_rebinding(BindingProposal(query_id="Q1", value="Bea", support_refs=[replacement]),
                           window_index=2)
        assert rt.state.executions["Q2"].status == "ACTIVE"
        assert rt.state.executions["Q2"].review_pending
        assert child_id in rt.state.facts
        assert not any(link.query_id == "Q2" for link in rt.state.links.values())
        parent_link = next(link for link in rt.state.links.values() if link.query_id == "Q1")
        assert parent_link.value == "Bea"
    finally:
        store.close()


def test_same_value_support_replacement_refreshes_descendant_proof_without_invalidation():
    store, rt = runtime()
    try:
        old_parent, child_id = bind_parent_and_admit_child(
            rt, child_text="Ada was born in London.", child_ref="s3")
        rt.apply_binding(BindingProposal(query_id="Q2", value="London", support_refs=[child_id]),
                         window_index=1)
        ingest(rt, "Q1 | s4 | Bea also confirms that Ada directed Fern.", window=2)
        new_parent = next(fid for fid in rt.state.accepted_ids("Q1") if fid != old_parent)
        rt.apply_rebinding(BindingProposal(query_id="Q1", value="Ada", support_refs=[new_parent]),
                           window_index=2)
        child = rt.state.executions["Q2"]
        assert child.status == "RESOLVED"
        assert child.result == "London"
        assert new_parent in child.support_fact_ids
        assert old_parent not in child.support_fact_ids
        child_link = next(link for link in rt.state.links.values() if link.query_id == "Q2")
        assert child_link.support_fact_ids == sorted([new_parent, child_id])
        assert not any(event.event_type == "QUERY_STATE_UPDATED"
                       and event.payload["query_id"] == "Q2"
                       and event.payload["state"]["status"] == "DORMANT"
                       for event in store.list_runtime_events("v07"))
    finally:
        store.close()


def test_event_replay_preserves_confirmed_links():
    store, rt = runtime()
    try:
        _, child_id = bind_parent_and_admit_child(
            rt, child_text="Ada was born in London.", child_ref="s3")
        rt.apply_binding(BindingProposal(query_id="Q2", value="London", support_refs=[child_id]),
                         window_index=1)
        events = [RuntimeEvent.model_validate(event.model_dump(mode="json"))
                  for event in store.list_runtime_events("v07")]
        restored = replay_subqueries(events, plan())
        assert restored.export() == rt.state.export()
    finally:
        store.close()
