import asyncio
import json

from delaybind_core import Manifest, ManifestEntry, SQLiteEventStore
from delaybind_core.data import CanonicalSample, build_manifest, canonicalize_record
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.schema import QueryPlan


class FakeClient:
    def __init__(self):
        self.calls = []

    async def complete(self, *, run_id, interface, messages, response_schema=None, extra=None):
        self.calls.append(interface)
        if interface == "PLAN":
            return json.dumps(
                {
                    "plan_id": "mock-plan",
                    "patterns": [
                        {"id": "director", "subject": "Film A", "relation": "DIRECTOR", "object": "?d"},
                        {"id": "birth", "subject": "?d", "relation": "TEMPORAL_ORDER_KEY", "object": "?year"},
                    ],
                    "answer_contract": {
                        "target": "?year",
                        "type": "NUMBER",
                        "cardinality": "SINGLE",
                        "normalization": "IDENTITY"
                    }
                }
            )
        if interface == "UPDATE":
            if "<UPDATE_CALLBACK" in messages[0]["content"]:
                return json.dumps({"matches": []})
            if "q1:d1:s1" in messages[0]["content"]:
                return json.dumps(
                    {
                        "events": [
                            {
                                "event_id": "director",
                                "source_ref": "q1:d1:s1",
                                "subject": "Film A",
                                "concrete_relation": "director",
                                "matched_family": "DIRECTOR",
                                "object": "Martin Lee",
                                "span_hint": "directed by Martin Lee",
                                "proposed_action": "COMMIT",
                                "source_order": 1,
                            }
                        ]
                    }
                )
            return json.dumps(
                {
                    "events": [
                        {
                            "event_id": "birth",
                            "source_ref": "q1:d1:s0",
                            "subject": "Martin Lee",
                            "concrete_relation": "birth_year",
                            "matched_family": "TEMPORAL_ORDER_KEY",
                            "object": 1948,
                            "span_hint": "born in 1948",
                            "proposed_action": "DEFER",
                            "source_order": 0,
                        }
                    ]
                }
            )
        if interface == "VERIFY":
            claim = json.loads(messages[0]["content"].split("Candidate claim:\n", 1)[1].split("\n\nRaw neighborhood:", 1)[0])
            neighborhood = json.loads(messages[0]["content"].split("Raw neighborhood:\n", 1)[1].split("\n\nReturn only", 1)[0])
            return json.dumps({
                "claim_id": claim["claim_id"], "status": "ACCEPT", "reason": "explicit statement",
                "supporting_source_refs": [neighborhood[0]["source_ref"]],
                "supporting_text": neighborhood[0]["text"],
            })
        raise AssertionError(f"unexpected interface: {interface}")


def test_runner_executes_mock_plan_update_and_verify():
    sample = canonicalize_record(
        {
            "id": "q1",
            "question": "Who is the director of Film A?",
            "context": [["Film A", ["Martin Lee was born in 1948.", "Film A was directed by Martin Lee."]]],
        }
    )
    # Reverse the actual text order to exercise the deferred path.
    manifest = Manifest.from_entries(
        manifest_id="mock",
        dataset_id="fixture",
        sample_id="q1",
        seed=4,
        entries=[
            ManifestEntry(
                source_ref="q1:d1:s0", dataset_id="fixture", sample_id="q1", document_id="d1",
                title="Film A", sentence_id=0, text="Martin Lee was born in 1948.", stream_position=0,
            ),
            ManifestEntry(
                source_ref="q1:d1:s1", dataset_id="fixture", sample_id="q1", document_id="d1",
                title="Film A", sentence_id=1, text="Film A was directed by Martin Lee.", stream_position=1,
            ),
        ],
    )
    store = SQLiteEventStore()
    result = asyncio.run(
        V5Runner(FakeClient(), config=RunnerConfig(chunk_size=1)).run(
            run_id="mock-run", sample=sample, manifest=manifest, store=store
        )
    )
    assert result["state"]["bindings"]["?d"] == "Martin Lee"
    assert not result["state"]["deferred"]
    assert result["state"]["status"] == "ANSWERED"
    assert result["evidence_pack"]["answer_value"] == 1948
    snapshot = store.latest_snapshot("mock-run")
    assert snapshot is not None
    assert snapshot["payload"]["cursor_position"] == 2


class RetryPlanClient:
    def __init__(self):
        self.calls = []
        self.plan_attempts = 0

    async def complete(self, *, run_id, interface, messages, response_schema=None, extra=None):
        self.calls.append(interface)
        if interface != "PLAN":
            raise AssertionError(f"unexpected interface: {interface}")
        self.plan_attempts += 1
        if self.plan_attempts == 1:
            return "{}"
        return json.dumps(
            {
                "plan_id": "retried-plan",
                "patterns": [
                    {"id": "director", "subject": "Film A", "relation": "DIRECTOR", "object": "?director"}
                ],
                "answer_contract": {"target": "?director", "type": "ENTITY"},
            }
        )


def test_runner_retries_schema_invalid_plan_once():
    sample = CanonicalSample(sample_id="q-retry", question="Who directed Film A?", documents=[])
    manifest = Manifest.from_entries(
        manifest_id="empty", dataset_id="2wiki", sample_id="q-retry", seed=4, entries=[]
    )
    client = RetryPlanClient()
    result = asyncio.run(
        V5Runner(client, config=RunnerConfig(max_windows=0)).run(
            run_id="retry-run", sample=sample, manifest=manifest, store=SQLiteEventStore()
        )
    )
    assert client.calls == ["PLAN", "PLAN"]
    assert result["state"]["status"] == "INSUFFICIENT"


def test_runner_stops_at_model_call_budget():
    sample = canonicalize_record(
        {
            "id": "q-budget",
            "question": "Who directed Film A?",
            "context": [["Film A", ["Film A was directed by Martin Lee."]]],
        }
    )
    manifest = build_manifest(sample, dataset_id="2wiki")
    client = FakeClient()
    result = asyncio.run(
        V5Runner(client, config=RunnerConfig(max_model_calls=1)).run(
            run_id="budget-run", sample=sample, manifest=manifest, store=SQLiteEventStore()
        )
    )
    # The PLAN consumes the one-call budget, so UPDATE is never issued.
    assert result["state"]["status"] == "RESOURCE_LIMIT"


def test_runner_marks_single_window_as_invalid_streaming_protocol():
    sample = canonicalize_record(
        {
            "id": "q-streaming",
            "question": "Who directed Film A?",
            "context": [["Film A", ["Film A was directed by Martin Lee."]]],
        }
    )
    result = asyncio.run(
        V5Runner(FakeClient(), config=RunnerConfig(chunk_size=100, min_streaming_windows=2)).run(
            run_id="single-window-run",
            sample=sample,
            manifest=build_manifest(sample),
            store=SQLiteEventStore(),
        )
    )
    assert result["windows_processed"] == 1
    assert result["streaming_protocol_valid"] is False
    assert result["state"]["status"] == "INSUFFICIENT"
    assert result["state"]["reason_codes"] == ["STREAMING_PROTOCOL_TOO_SHORT:1<2"]


class ExpandingVerifyClient:
    def __init__(self):
        self.verify_calls = 0

    async def complete(self, *, run_id, interface, messages, response_schema=None, extra=None):
        if interface == "UPDATE":
            window = json.loads(messages[0]["content"].split("Current window:\n", 1)[1].split("\n\nReturn only", 1)[0])
            return json.dumps(
                {
                    "events": [
                        {
                            "event_id": "director-expanded",
                            "source_ref": window[0]["source_ref"],
                            "subject": "Film A",
                            "concrete_relation": "director",
                            "matched_family": "director",
                            "object": "Martin Lee",
                            "span_hint": "directed by Martin Lee",
                            "source_order": window[0]["stream_position"],
                        }
                    ]
                }
            )
        if interface == "VERIFY":
            self.verify_calls += 1
            claim = json.loads(messages[0]["content"].split("Candidate claim:\n", 1)[1].split("\n\nRaw neighborhood:", 1)[0])
            status = "NEED_MORE_CONTEXT" if self.verify_calls == 1 else "ACCEPT"
            result = {"claim_id": claim["claim_id"], "status": status, "reason": "fixture verification"}
            if status == "ACCEPT":
                neighborhood = json.loads(messages[0]["content"].split("Raw neighborhood:\n", 1)[1].split("\n\nReturn only", 1)[0])
                result.update({
                    "supporting_source_refs": [neighborhood[0]["source_ref"]],
                    "supporting_text": neighborhood[0]["text"],
                })
            return json.dumps(result)
        raise AssertionError(interface)


def test_runner_expands_verify_context_once_on_need_more_context():
    sample = canonicalize_record(
        {
            "id": "q-expand",
            "question": "Who directed Film A?",
            "context": [["Film A", ["Film A was directed by Martin Lee.", "one", "two", "three"]]],
        }
    )
    manifest = build_manifest(sample)
    plan = QueryPlan(
        plan_id="expand-plan",
        patterns=[{"id": "director", "subject": "Film A", "relation": "director", "object": "?director"}],
        answer_contract={"target": "?director", "type": "ENTITY"},
    )
    client = ExpandingVerifyClient()
    result = asyncio.run(
        V5Runner(client, config=RunnerConfig(chunk_size=100, max_verify_expansions=1)).run(
            run_id="expand-run", sample=sample, manifest=manifest,
            store=SQLiteEventStore(), plan=plan,
        )
    )
    assert client.verify_calls == 2
    assert result["state"]["status"] == "ANSWERED"


class TargetFreeEvidenceClient:
    def __init__(self):
        self.calls = []

    async def complete(self, *, run_id, interface, messages, response_schema=None, extra=None):
        self.calls.append(interface)
        if interface == "PLAN":
            return json.dumps(
                {
                    "plan_id": "target-free",
                    "patterns": [
                        {"id": "director", "subject": "Film A", "relation": "director", "object": "?director"}
                    ],
                }
            )
        if interface == "UPDATE":
            window = json.loads(messages[0]["content"].split("Current window:\n", 1)[1].split("\n\nReturn only", 1)[0])
            return json.dumps(
                {
                    "events": [
                        {
                            "event_id": "target-free-director",
                            "source_ref": window[0]["source_ref"],
                            "subject": "Film A",
                            "concrete_relation": "director",
                            "matched_family": "director",
                            "object": "Martin Lee",
                            "span_hint": "directed by Martin Lee",
                            "source_order": window[0]["stream_position"],
                        }
                    ]
                }
            )
        if interface == "VERIFY":
            claim = json.loads(messages[0]["content"].split("Candidate claim:\n", 1)[1].split("\n\nRaw neighborhood:", 1)[0])
            neighborhood = json.loads(messages[0]["content"].split("Raw neighborhood:\n", 1)[1].split("\n\nReturn only", 1)[0])
            return json.dumps({
                "claim_id": claim["claim_id"], "status": "ACCEPT", "reason": "explicit statement",
                "supporting_source_refs": [neighborhood[0]["source_ref"]],
                "supporting_text": neighborhood[0]["text"],
            })
        if interface == "ANSWER":
            return json.dumps(
                {
                    "answer": "Martin Lee",
                    "answer_type": "ENTITY",
                    "source_refs": ["q-evidence:d00000:s0"],
                }
            )
        raise AssertionError(interface)


def test_target_free_evidence_answer_uses_verified_graph_only():
    sample = canonicalize_record(
        {
            "id": "q-evidence",
            "question": "Who directed Film A?",
            "context": [["Film A", ["Film A was directed by Martin Lee."]]],
        }
    )
    client = TargetFreeEvidenceClient()
    result = asyncio.run(
        V5Runner(client, config=RunnerConfig(answer_mode="evidence", chunk_size=100)).run(
            run_id="evidence-run",
            sample=sample,
            manifest=build_manifest(sample),
            store=SQLiteEventStore(),
        )
    )
    assert client.calls == ["PLAN", "UPDATE", "VERIFY", "ANSWER"]
    assert result["state"]["status"] == "ANSWERED"
    assert result["state"]["plan"]["answer_contract"]["target"] is None
    assert result["evidence_pack"]["answer_value"] == "Martin Lee"


class CallbackRecoveryClient:
    async def complete(self, *, run_id, interface, messages, response_schema=None, extra=None):
        content = messages[0]["content"]
        if interface == "UPDATE" and "<UPDATE_CALLBACK" in content:
            entries = json.loads(content.split("Read-prefix entries:\n", 1)[1].split("\n\nReturn only", 1)[0])
            entry = next(item for item in entries if "born in 1948" in item["text"])
            return json.dumps({
                "matches": [{
                    "source_ref": entry["source_ref"],
                    "subject": "Martin Lee",
                    "object": 1948,
                    "supporting_text": "born in 1948",
                }]
            })
        if interface == "UPDATE":
            entries = json.loads(content.split("Current window:\n", 1)[1].split("\n\nReturn only", 1)[0])
            entry = entries[0]
            if "directed by" not in entry["text"]:
                return json.dumps({"events": []})
            return json.dumps({
                "events": [{
                    "event_id": "e01",
                    "source_ref": entry["source_ref"],
                    "subject": "Film A",
                    "concrete_relation": "director",
                    "matched_family": "DIRECTOR",
                    "object": "Martin Lee",
                    "pattern_hint": "director",
                    "span_hint": "directed by Martin Lee",
                    "source_order": entry["stream_position"],
                }]
            })
        if interface == "VERIFY":
            claim = json.loads(content.split("Candidate claim:\n", 1)[1].split("\n\nRaw neighborhood:", 1)[0])
            entries = json.loads(content.split("Raw neighborhood:\n", 1)[1].split("\n\nReturn only", 1)[0])
            entry = next(item for item in entries if item["source_ref"] == claim["evidence_assertions"][0]["source_ref"])
            return json.dumps({
                "claim_id": claim["claim_id"],
                "status": "ACCEPT",
                "reason": "explicit statement",
                "supporting_source_refs": [entry["source_ref"]],
                "supporting_text": entry["text"],
            })
        raise AssertionError(interface)


def test_binding_activation_recovers_missed_fact_from_read_prefix():
    sample = canonicalize_record({
        "id": "q-callback",
        "question": "When was the director of Film A born?",
        "context": [["Martin Lee", ["Martin Lee was born in 1948."]], ["Film A", ["Film A was directed by Martin Lee."]]],
    })
    plan = QueryPlan(
        plan_id="callback-plan",
        patterns=[
            {"id": "director", "subject": "Film A", "relation": "DIRECTOR", "object": "?director"},
            {"id": "birth", "subject": "?director", "relation": "TEMPORAL_ORDER_KEY", "object": "?year"},
        ],
        answer_contract={"target": "?year", "type": "NUMBER"},
    )
    result = asyncio.run(V5Runner(
        CallbackRecoveryClient(),
        config=RunnerConfig(chunk_size=1, max_targeted_updates=4),
    ).run(
        run_id="callback-recovery",
        sample=sample,
        manifest=build_manifest(sample),
        store=SQLiteEventStore(),
        plan=plan,
    ))
    assert result["state"]["status"] == "ANSWERED"
    assert result["evidence_pack"]["answer_value"] == 1948
    assert result["state"]["edge_status"] == {"director": "SATISFIED", "birth": "SATISFIED"}
