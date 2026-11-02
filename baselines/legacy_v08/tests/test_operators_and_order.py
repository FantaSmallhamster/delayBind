from delaybind_core import Action, EvidenceRuntime, Manifest, ManifestEntry, QueryPlan, RawArchive, SQLiteEventStore, TripleEvent
from delaybind_core.operators import execute_operator


def test_typed_operators_are_deterministic():
    assert execute_operator("EARLIER", "1948", "1952") is True
    assert execute_operator("YOUNGER", "1952", "1948") is True
    assert execute_operator("COUNT", ["a", "a", "b"]) == 2
    assert execute_operator("INTERSECTION", ["a", "b"], ["b", "c"]) == ["b"]
    assert execute_operator("COMPARE", 1948, 1952, "LT") is True


def test_runtime_executes_count_and_projects_operator_output():
    entry = ManifestEntry(
        source_ref="q:d:s0", dataset_id="d", sample_id="q", document_id="doc",
        title="T", sentence_id=0, text="Film A has two directors.", stream_position=0,
    )
    plan = QueryPlan(
        plan_id="operator-plan",
        patterns=[{"id": "p1", "subject": "Film A", "relation": "director", "object": "?director"}],
        operators=[
            {"id": "count", "type": "COUNT", "inputs": [["A", "B"]], "params": {"output": "?count"}},
        ],
        answer_contract={"target": "?count", "type": "NUMBER"},
    )
    store = SQLiteEventStore()
    archive = RawArchive(store, "operator-run")
    archive.append([entry])
    runtime = EvidenceRuntime(run_id="operator-run", plan=plan, archive=archive, store=store)
    # Satisfy the required evidence pattern directly for this EOS operator test.
    runtime.apply_event(TripleEvent(
        event_id="director", source_ref=entry.source_ref, subject="Film A",
        concrete_relation="director", object="A", proposed_action=Action.COMMIT, source_order=0,
    ))
    pack = runtime.finalize()
    assert pack is not None
    assert pack.answer_value == 2
    assert pack.operator_trace[0]["type"] == "COUNT"


def test_runtime_reorders_model_events_by_source_order():
    entries = [
        ManifestEntry(
            source_ref="q:d:s0", dataset_id="d", sample_id="q", document_id="doc",
            title="T", sentence_id=0, text="Film A was directed by Martin Lee.", stream_position=0,
        ),
        ManifestEntry(
            source_ref="q:d:s1", dataset_id="d", sample_id="q", document_id="doc",
            title="T", sentence_id=1, text="Martin Lee was born in 1948.", stream_position=1,
        ),
    ]
    plan = QueryPlan(
        plan_id="p",
        patterns=[
            {"id": "director", "subject": "Film A", "relation": "DIRECTOR", "object": "?d"},
            {"id": "birth", "subject": "?d", "relation": "TEMPORAL_ORDER_KEY", "object": "?year"},
        ],
    )
    store = SQLiteEventStore()
    archive = RawArchive(store, "run")
    archive.append(entries)
    runtime = EvidenceRuntime(run_id="run", plan=plan, archive=archive, store=store)
    events = [
        TripleEvent(
            event_id="birth", source_ref="q:d:s1", subject="Martin Lee", concrete_relation="birth_year",
            matched_family="TEMPORAL_ORDER_KEY", object=1948, source_order=1, proposed_action=Action.DEFER,
        ),
        TripleEvent(
            event_id="director", source_ref="q:d:s0", subject="Film A", concrete_relation="director",
            matched_family="DIRECTOR", object="Martin Lee", source_order=0, proposed_action=Action.COMMIT,
        ),
    ]
    results = runtime.apply_events(events)
    assert [result.claim.subject for result in results] == ["Film A", "Martin Lee"]
    assert results[1].applied_action == Action.COMMIT


def test_runtime_uses_manifest_position_instead_of_model_position():
    entries = [
        ManifestEntry(
            source_ref="q:d:s0", dataset_id="d", sample_id="q", document_id="doc",
            title="T", sentence_id=0, text="Film A was directed by Martin Lee.", stream_position=0,
        ),
    ]
    plan = QueryPlan(plan_id="p", patterns=[
        {"id": "director", "subject": "Film A", "relation": "DIRECTOR", "object": "?d"}
    ])
    store = SQLiteEventStore()
    archive = RawArchive(store, "run")
    archive.append(entries)
    runtime = EvidenceRuntime(run_id="run", plan=plan, archive=archive, store=store)
    runtime.apply_event(TripleEvent(
        event_id="e", source_ref="q:d:s0", subject="Film A", concrete_relation="director",
        matched_family="DIRECTOR", object="Martin Lee", source_order=999,
    ))
    assert any(e.event_type == "EVENT_SOURCE_ORDER_MISMATCH" for e in store.list_runtime_events("run"))
