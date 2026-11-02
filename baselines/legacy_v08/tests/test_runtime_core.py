from delaybind_core import (
    Action,
    EvidenceRuntime,
    Manifest,
    ManifestEntry,
    Polarity,
    QueryPlan,
    RawArchive,
    RelationSpec,
    SQLiteEventStore,
    TripleEvent,
    VerifyResult,
    VerifyStatus,
)
from delaybind_core.schema import QueryEdgeStatus


def make_manifest() -> Manifest:
    # Reverse evidence order: the attribute appears before the binding fact.
    entries = [
        ManifestEntry(
            source_ref="q1:d1:s1",
            dataset_id="fixture",
            sample_id="q1",
            document_id="d1",
            title="Film A",
            sentence_id=1,
            text="Martin Lee was born in 1948.",
            stream_position=0,
        ),
        ManifestEntry(
            source_ref="q1:d1:s2",
            dataset_id="fixture",
            sample_id="q1",
            document_id="d1",
            title="Film A",
            sentence_id=2,
            text="Film A was directed by Martin Lee.",
            stream_position=1,
        ),
    ]
    return Manifest.from_entries(
        manifest_id="fixture-reverse",
        dataset_id="fixture",
        sample_id="q1",
        seed=4,
        entries=entries,
    )


def make_runtime():
    plan = QueryPlan(
        plan_id="plan-1",
        relation_specs=[
            RelationSpec(
                id="R_DIRECTOR",
                description="the person who directed the film",
                subject_type="Film",
                object_type="Person",
            ),
            RelationSpec(
                id="R_TIME",
                description="the person's temporal ordering value",
                subject_type="Person",
                object_type="YEAR",
            ),
        ],
        patterns=[
            {"id": "T1", "subject": "Film A", "relation": "DIRECTOR", "object": "?d_A"},
            {
                "id": "T2",
                "subject": "?d_A",
                "relation": "TEMPORAL_ORDER_KEY",
                "object": "?t_A",
            },
        ],
    )
    store = SQLiteEventStore()
    archive = RawArchive(store, "run-1")
    manifest = make_manifest()
    archive.append(manifest.entries)
    return EvidenceRuntime(run_id="run-1", plan=plan, archive=archive, store=store), store


def test_reverse_delayed_binding_promotes_early_fact_and_replays():
    runtime, store = make_runtime()
    early = TripleEvent(
        event_id="e-early",
        source_ref="q1:d1:s1",
        subject="Martin Lee",
        concrete_relation="birth_year",
        matched_family="TEMPORAL_ORDER_KEY",
        object=1948,
        proposed_action=Action.DEFER,
        source_order=0,
    )
    deferred = runtime.apply_event(early)
    assert deferred.applied_action == Action.DEFER
    assert deferred.claim is not None
    assert deferred.claim.claim_id in runtime.state.deferred

    binding = TripleEvent(
        event_id="e-binding",
        source_ref="q1:d1:s2",
        subject="Film A",
        concrete_relation="director",
        matched_family="DIRECTOR",
        object="Martin Lee",
        proposed_action=Action.COMMIT,
        source_order=1,
    )
    committed = runtime.apply_event(binding)
    assert committed.applied_action == Action.COMMIT
    assert runtime.state.bindings["?d_A"] == "Martin Lee"
    assert len(committed.deferred_matches) == 1

    claim_id = committed.deferred_matches[0].claim_id
    runtime.apply_verification(
        VerifyResult(claim_id=claim_id, status=VerifyStatus.ACCEPT)
    )
    assert claim_id not in runtime.state.deferred
    assert runtime.state.verified[claim_id].disposition == "PROMOTED"
    assert runtime.state.bindings["?t_A"] == 1948

    replayed = __import__("delaybind_core.replay", fromlist=["replay_events"]).replay_events(
        store.list_runtime_events("run-1")
    )
    assert replayed.bindings == runtime.state.bindings
    assert replayed.edge_status == runtime.state.edge_status
    assert replayed.edge_support == runtime.state.edge_support
    assert set(replayed.verified) == set(runtime.state.verified)
    assert not replayed.deferred


def test_open_query_graph_activates_downstream_edge_and_tracks_support():
    runtime, _ = make_runtime()
    assert runtime.state.edge_status == {
        "T1": QueryEdgeStatus.ACTIVE,
        "T2": QueryEdgeStatus.DORMANT,
    }
    binding = runtime.apply_event(
        TripleEvent(
            event_id="graph-binding",
            source_ref="q1:d1:s2",
            subject="Film A",
            concrete_relation="director",
            matched_family="DIRECTOR",
            object="Martin Lee",
            source_order=1,
        )
    )
    assert binding.applied_action == Action.COMMIT
    assert runtime.state.edge_status["T1"] == QueryEdgeStatus.SATISFIED
    assert runtime.state.edge_status["T2"] == QueryEdgeStatus.ACTIVE
    assert binding.claim is not None
    assert runtime.state.edge_support == {"T1": [binding.claim.claim_id]}
    assert runtime.state.binding_provenance["?d_A"]["source_ref"] == "q1:d1:s2"


def test_cross_window_deferred_promotion_is_distinct_from_event_order_artifact():
    runtime, store = make_runtime()
    runtime.register_read_window(0, ["q1:d1:s1"])
    early = runtime.apply_event(
        TripleEvent(
            event_id="cross-window-early",
            source_ref="q1:d1:s1",
            subject="Martin Lee",
            concrete_relation="birth_year",
            matched_family="TEMPORAL_ORDER_KEY",
            object=1948,
            source_order=0,
        )
    )
    assert early.claim is not None
    runtime.register_read_window(1, ["q1:d1:s2"])
    runtime.apply_event(
        TripleEvent(
            event_id="cross-window-binding",
            source_ref="q1:d1:s2",
            subject="Film A",
            concrete_relation="director",
            matched_family="DIRECTOR",
            object="Martin Lee",
            source_order=1,
        )
    )
    runtime.apply_verification(
        VerifyResult(claim_id=early.claim.claim_id, status=VerifyStatus.ACCEPT)
    )
    promoted = [
        event for event in store.list_runtime_events("run-1")
        if event.event_type == "CROSS_WINDOW_DEFERRED_PROMOTED"
    ]
    assert len(promoted) == 1
    assert promoted[0].payload["deferred_source_ref"] == "q1:d1:s1"
    assert promoted[0].payload["binding_source_ref"] == "q1:d1:s2"
    assert promoted[0].payload["window_distance"] == 1


def test_finalize_refuses_when_required_pattern_is_missing():
    runtime, _ = make_runtime()
    assert runtime.finalize() is None
    assert runtime.state.status == "INSUFFICIENT"
    assert runtime.state.reason_codes == ["REQUIRED_PATTERN_MISSING:T1,T2"]


def test_graph_projection_is_compact_and_bounded():
    runtime, _ = make_runtime()
    runtime.state.bindings["?d_A"] = "Martin Lee"
    runtime.state.verified["claim-a"] = runtime._claim_from_event(
        TripleEvent(
            event_id="compact", source_ref="q1:d1:s2", subject="Film A",
            concrete_relation="director", matched_family="DIRECTOR", object="Martin Lee",
        )
    )
    projection = runtime.state.graph_projection(max_claims=1)
    assert projection["bindings"] == {"?d_A": "Martin Lee"}
    assert set(projection["claims"][0]) == {
        "claim_id", "subject", "relation", "object", "qualifiers",
        "polarity", "modality", "matched_pattern_id",
    }
    assert "evidence_assertions" not in projection["claims"][0]


def test_deferred_lookup_filters_by_entity_and_relation_family():
    runtime, _ = make_runtime()
    for event_id, subject, family, relation, value in [
        ("right", "Martin Lee", "TEMPORAL_ORDER_KEY", "birth_year", 1948),
        ("wrong-family", "Martin Lee", "LOCATION", "lives_in", "Paris"),
        ("wrong-person", "Robert Lee", "TEMPORAL_ORDER_KEY", "birth_year", 1952),
    ]:
        runtime.apply_event(
            TripleEvent(
                event_id=event_id,
                source_ref="q1:d1:s1",
                subject=subject,
                concrete_relation=relation,
                matched_family=family,
                object=value,
                proposed_action=Action.DEFER,
                source_order=0,
            )
        )
    result = runtime.apply_event(
        TripleEvent(
            event_id="binding-filter",
            source_ref="q1:d1:s2",
            subject="Film A",
            concrete_relation="director",
            matched_family="DIRECTOR",
            object="Martin Lee",
            proposed_action=Action.COMMIT,
            source_order=1,
        )
    )
    assert [(claim.subject, claim.relation) for claim in result.deferred_matches] == [
        ("Martin Lee", "TEMPORAL_ORDER_KEY")
    ]


def test_pattern_hint_falls_back_to_entity_matched_same_relation_pattern():
    base_runtime, store = make_runtime()
    plan = QueryPlan(
        plan_id="hint-plan",
        patterns=[
            {"id": "FilmA", "subject": "Film A", "relation": "DIRECTOR", "object": "?director_a"},
            {"id": "FilmB", "subject": "Film B", "relation": "DIRECTOR", "object": "?director_b"},
        ],
    )
    runtime = EvidenceRuntime(
        run_id="hint-run", plan=plan, archive=base_runtime.archive, store=store
    )
    result = runtime.apply_event(
        TripleEvent(
            event_id="wrong-hint",
            source_ref="q1:d1:s2",
            subject="Film A",
            concrete_relation="director",
            matched_family="DIRECTOR",
            object="Martin Lee",
            pattern_hint="FilmB",
            source_order=1,
        )
    )
    assert result.pattern_id == "FilmA"
    assert result.applied_action == Action.COMMIT


def test_pattern_endpoint_matches_explicit_question_alias():
    base_runtime, store = make_runtime()
    plan = QueryPlan(
        plan_id="alias-plan",
        patterns=[
            {
                "id": "alias",
                "subject": "Maurice of Nassau",
                "relation": "father",
                "object": "?father",
                "qualifiers": {
                    "subject_question_anchor": "Maurice, Prince Of Orange",
                    "subject_aliases": ["Maurice, Prince Of Orange"],
                },
            }
        ],
    )
    runtime = EvidenceRuntime(
        run_id="alias-run", plan=plan, archive=base_runtime.archive, store=store
    )
    result = runtime.apply_event(
        TripleEvent(
            event_id="alias-event",
            source_ref="q1:d1:s2",
            subject="Maurice, Prince Of Orange",
            concrete_relation="father",
            matched_family="father",
            object="William the Silent",
            source_order=1,
        )
    )
    assert result.pattern_id == "alias"
    assert result.applied_action == Action.COMMIT


def test_path_consistent_candidates_select_the_supported_complete_branch():
    entries = [
        ManifestEntry(
            source_ref="path:d1:s0", dataset_id="fixture", sample_id="path",
            document_id="d1", title="Song A", sentence_id=0,
            text="Song A was performed by Cover Artist.", stream_position=0,
        ),
        ManifestEntry(
            source_ref="path:d1:s1", dataset_id="fixture", sample_id="path",
            document_id="d1", title="Song A", sentence_id=1,
            text="Song A was originally performed by Original Artist.", stream_position=1,
        ),
        ManifestEntry(
            source_ref="path:d2:s0", dataset_id="fixture", sample_id="path",
            document_id="d2", title="Original Artist", sentence_id=0,
            text="Original Artist is Canadian.", stream_position=2,
        ),
    ]
    manifest = Manifest.from_entries(
        manifest_id="path-candidates", dataset_id="fixture", sample_id="path",
        seed=4, entries=entries,
    )
    store = SQLiteEventStore()
    archive = RawArchive(store, "path-run")
    archive.append(manifest.entries)
    plan = QueryPlan(
        plan_id="path-plan",
        patterns=[
            {
                "id": "performer", "subject": "Song A", "relation": "performer",
                "object": "?performer", "qualifiers": {
                    "binding_policy": "PATH_CONSISTENT", "candidate_output": "?performer"
                },
            },
            {
                "id": "nationality", "subject": "?performer",
                "relation": "country of citizenship", "object": "?country",
            },
        ],
        answer_contract={"target": "?country", "type": "NATIONALITY"},
    )
    runtime = EvidenceRuntime(
        run_id="path-run", plan=plan, archive=archive, store=store
    )

    def apply(event_id, source_ref, subject, relation, obj, pattern_hint):
        return runtime.apply_event(
            TripleEvent(
                event_id=event_id, source_ref=source_ref, subject=subject,
                concrete_relation=relation, matched_family=relation, object=obj,
                pattern_hint=pattern_hint,
            )
        )

    apply("cover", "path:d1:s0", "Song A", "performer", "Cover Artist", "performer")
    apply("original", "path:d1:s1", "Song A", "performer", "Original Artist", "performer")
    assert runtime.state.bindings["?performer"] == "Cover Artist"
    assert [item["value"] for item in runtime.state.binding_candidates["?performer"]] == [
        "Cover Artist", "Original Artist"
    ]
    apply(
        "country", "path:d2:s0", "Original Artist", "country of citizenship",
        "Canadian", "nationality",
    )
    assert runtime.state.bindings["?performer"] == "Original Artist"
    assert runtime.state.bindings["?country"] == "Canadian"
    pack = runtime.finalize()
    assert pack is not None
    assert {(claim.subject, claim.object) for claim in pack.claims} == {
        ("Song A", "Original Artist"), ("Original Artist", "Canadian")
    }


def test_document_title_fallback_does_not_reverse_a_directed_edge():
    base_runtime, store = make_runtime()
    plan = QueryPlan(
        plan_id="direction-plan",
        patterns=[
            {"id": "director", "subject": "Film A", "relation": "DIRECTOR", "object": "?director"}
        ],
    )
    runtime = EvidenceRuntime(
        run_id="direction-run", plan=plan, archive=base_runtime.archive, store=store
    )
    result = runtime.apply_event(
        TripleEvent(
            event_id="reversed-director",
            source_ref="q1:d1:s2",
            subject="Martin Lee",
            concrete_relation="director",
            matched_family="DIRECTOR",
            object="Film A",
            source_order=1,
        )
    )
    assert result.applied_action == Action.SKIP
    assert "?director" not in runtime.state.bindings


def test_runtime_rejects_cross_document_event_before_verify():
    base_runtime, store = make_runtime()
    plan = QueryPlan(
        plan_id="source-local-plan",
        patterns=[
            {
                "id": "performer",
                "subject": "Unrelated Song",
                "relation": "DIRECTOR",
                "object": "?person",
            }
        ],
    )
    runtime = EvidenceRuntime(
        run_id="source-local-run",
        plan=plan,
        archive=base_runtime.archive,
        store=store,
        require_source_span=True,
    )
    result = runtime.apply_event(
        TripleEvent(
            event_id="cross-document",
            source_ref="q1:d1:s1",
            subject="Unrelated Song",
            concrete_relation="director",
            matched_family="DIRECTOR",
            object="Martin Lee",
            span_hint="Martin Lee",
            source_order=0,
        )
    )
    assert result.applied_action == Action.SKIP
    assert any(
        event.event_type == "SOURCE_LOCAL_CANDIDATE_REJECTED"
        for event in store.list_runtime_events("source-local-run")
    )


def test_deferred_lookup_supports_binding_on_pattern_object_side():
    entries = [
        ManifestEntry(
            source_ref="q1:d1:s1", dataset_id="fixture", sample_id="q1", document_id="d1",
            title="Film A", sentence_id=1, text="1948 is the birth year of Martin Lee.", stream_position=0,
        ),
        ManifestEntry(
            source_ref="q1:d1:s2", dataset_id="fixture", sample_id="q1", document_id="d1",
            title="Film A", sentence_id=2, text="Martin Lee directed Film A.", stream_position=1,
        ),
    ]
    manifest = Manifest.from_entries(
        manifest_id="object-side", dataset_id="fixture", sample_id="q1", seed=4, entries=entries
    )
    plan = QueryPlan(
        plan_id="object-side-plan",
        patterns=[
            {"id": "director", "subject": "?person", "relation": "DIRECTOR", "object": "Film A"},
            {"id": "birth", "subject": "?year", "relation": "BIRTH_OF", "object": "?person"},
        ],
    )
    store = SQLiteEventStore()
    archive = RawArchive(store, "object-side-run")
    archive.append(entries)
    runtime = EvidenceRuntime(run_id="object-side-run", plan=plan, archive=archive, store=store)
    runtime.apply_event(TripleEvent(
        event_id="early-year", source_ref="q1:d1:s1", subject="1948", concrete_relation="birth_year",
        matched_family="BIRTH_OF", object="Martin Lee", source_order=0,
    ))
    result = runtime.apply_event(TripleEvent(
        event_id="director-object", source_ref="q1:d1:s2", subject="Martin Lee", concrete_relation="director",
        matched_family="DIRECTOR", object="Film A", source_order=1,
    ))
    assert [claim.claim_id for claim in result.deferred_matches]


def test_duplicate_model_event_is_idempotent():
    runtime, store = make_runtime()
    event = TripleEvent(
        event_id="stable-model-event",
        source_ref="q1:d1:s1",
        subject="Martin Lee",
        concrete_relation="birth_year",
        matched_family="TEMPORAL_ORDER_KEY",
        object=1948,
        source_order=0,
    )
    first = runtime.apply_event(event)
    event_count = len(store.list_runtime_events("run-1"))
    second = runtime.apply_event(event)
    assert first.claim == second.claim
    assert len(store.list_runtime_events("run-1")) == event_count
    assert len(runtime.state.deferred) == 1


def test_model_local_event_ids_are_namespaced_by_source_and_fact():
    runtime, store = make_runtime()
    first = TripleEvent(
        event_id="e01",
        source_ref="q1:d1:s1",
        subject="Martin Lee",
        concrete_relation="birth_year",
        matched_family="TEMPORAL_ORDER_KEY",
        object=1948,
        source_order=0,
    )
    second = TripleEvent(
        event_id="e01",
        source_ref="q1:d1:s2",
        subject="Film A",
        concrete_relation="director",
        matched_family="DIRECTOR",
        object="Martin Lee",
        source_order=1,
    )
    first_result, second_result = runtime.apply_events([first, second])
    assert first_result.claim is not None
    assert second_result.claim is not None
    assert first_result.claim.claim_id != second_result.claim.claim_id
    proposals = [
        event for event in store.list_runtime_events("run-1")
        if event.event_type == "TRIPLE_EVENT_PROPOSED"
    ]
    assert len(proposals) == 2


def test_verified_commit_does_not_bind_until_accept_and_reject_cleans_pending():
    base_runtime, store = make_runtime()
    runtime = EvidenceRuntime(
        run_id="run-verified-commit",
        plan=base_runtime.state.plan,
        archive=base_runtime.archive,
        store=store,
        verify_committed=True,
    )
    event = TripleEvent(
        event_id="pending-binding",
        source_ref="q1:d1:s2",
        subject="Film A",
        concrete_relation="director",
        matched_family="DIRECTOR",
        object="Martin Lee",
        source_order=1,
        proposed_action=Action.COMMIT,
    )
    result = runtime.apply_event(event)
    assert result.claim is not None
    assert result.claim.claim_id in runtime.state.pending
    assert "?d_A" not in runtime.state.bindings
    runtime.apply_verification(
        VerifyResult(claim_id=result.claim.claim_id, status=VerifyStatus.REJECT)
    )
    assert "?d_A" not in runtime.state.bindings
    assert result.claim.claim_id in runtime.state.rejected
    assert not runtime.state.pending


def test_no_defer_ablation_skips_unbound_candidate():
    base_runtime, store = make_runtime()
    runtime = EvidenceRuntime(
        run_id="run-no-defer",
        plan=base_runtime.state.plan,
        archive=base_runtime.archive,
        store=store,
        defer_unbound=False,
    )
    result = runtime.apply_event(
        TripleEvent(
            event_id="early-no-defer",
            source_ref="q1:d1:s1",
            subject="Martin Lee",
            concrete_relation="birth_year",
            matched_family="TEMPORAL_ORDER_KEY",
            object=1948,
            source_order=0,
        )
    )
    assert result.applied_action == Action.SKIP
    assert not runtime.state.deferred
    assert any(
        event.event_type == "CLAIM_SKIPPED" and event.payload.get("reason") == "DEFER_DISABLED"
        for event in store.list_runtime_events("run-no-defer")
    )


def test_no_future_source_access_is_rejected():
    runtime, store = make_runtime()
    # Replace the archive with a prefix containing only the first source.
    store.close()
    store = SQLiteEventStore()
    archive = RawArchive(store, "run-2")
    archive.append(make_manifest().entries[:1])
    runtime = EvidenceRuntime(
        run_id="run-2",
        plan=runtime.state.plan,
        archive=archive,
        store=store,
    )
    event = TripleEvent(
        event_id="future",
        source_ref="q1:d1:s2",
        subject="Film A",
        concrete_relation="director",
        matched_family="DIRECTOR",
        object="Martin Lee",
        source_order=1,
    )
    import pytest

    with pytest.raises(RuntimeError, match="not in the read prefix"):
        runtime.apply_event(event)


def test_batch_events_skip_future_source_without_aborting_window():
    runtime, store = make_runtime()
    future = TripleEvent(
        event_id="future-batch",
        source_ref="q1:d1:s2",
        subject="Film A",
        concrete_relation="director",
        matched_family="DIRECTOR",
        object="Martin Lee",
        source_order=1,
    )
    valid = TripleEvent(
        event_id="valid-batch",
        source_ref="q1:d1:s1",
        subject="Martin Lee",
        concrete_relation="birth_year",
        matched_family="TEMPORAL_ORDER_KEY",
        object=1948,
        source_order=0,
    )
    # Simulate a prefix containing only the first entry.
    store.close()
    store = SQLiteEventStore()
    archive = RawArchive(store, "batch-prefix")
    archive.append(make_manifest().entries[:1])
    runtime = EvidenceRuntime(run_id="batch-prefix", plan=runtime.state.plan, archive=archive, store=store)
    results = runtime.apply_events([future, valid])
    assert len(results) == 2
    assert any(event.event_type == "SOURCE_ACCESS_REJECTED" for event in store.list_runtime_events("batch-prefix"))
    assert len(runtime.state.deferred) == 1


def test_manifest_rejects_non_monotonic_stream_positions():
    import pytest

    first = ManifestEntry(
        source_ref="a",
        dataset_id="d",
        sample_id="s",
        document_id="doc",
        title="T",
        sentence_id=0,
        text="one",
        stream_position=1,
    )
    second = first.model_copy(update={"source_ref": "b", "sentence_id": 1, "stream_position": 0})
    with pytest.raises(ValueError, match="ascending"):
        Manifest.from_entries(
            manifest_id="bad",
            dataset_id="d",
            sample_id="s",
            seed=1,
            entries=[first, second],
        )
