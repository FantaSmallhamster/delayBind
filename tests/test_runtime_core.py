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
    assert set(replayed.verified) == set(runtime.state.verified)
    assert not replayed.deferred


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
