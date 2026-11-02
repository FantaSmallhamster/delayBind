"""UPDATE exposes only query/source/fact; Runtime owns admission scheduling."""

import json
import unittest

from pydantic import ValidationError

from delaybind_core.agent_prompts import reading_prompt, reading_repair_prompt
from delaybind_core.archive import RawArchive
from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.fact_protocol import BindingProposal, FactEvent, ProtocolError, parse_update
from delaybind_core.manifest import ManifestEntry
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.source_refs import VisibleSources
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subqueries import SubqueryRuntime
from delaybind_core.subquery_runner import run_subqueries


def plan():
    return QueryPlan(plan_id="routing", queries=[
        Subquery(id="Q1", template="Who directed Fern?", output="?director"),
        Subquery(id="Q2", template="Where was ?director born?", output="?birthplace", depends_on=["Q1"]),
    ])


def entry(ref, text, position):
    return ManifestEntry(source_ref=ref, sample_id="routing", dataset_id="test",
        document_id=ref, title=ref, sentence_id=0, stream_position=position, text=text)


class ThreeColumnParserTests(unittest.TestCase):
    def test_natural_language_multiple_refs_and_escaped_delimiters(self):
        result = parse_update(r'Q2 | D18@C0,D19@C0 | Ada wrote "A\|B".\nShe was born in Rome.')
        fact = result.facts[0]
        self.assertEqual(fact.source_refs, ["D18@C0", "D19@C0"])
        self.assertEqual(fact.text, 'Ada wrote "A|B".\nShe was born in Rome.')
        self.assertEqual(set(fact.model_dump()), {"query_id", "source_refs", "text"})

    def test_fourth_column_is_rejected_and_never_becomes_fact_text(self):
        for state in ["ACTIVE", "DORMANT", "COMMIT", "DEFER", "garbage"]:
            with self.subTest(state=state):
                with self.assertRaises(ProtocolError):
                    parse_update(f"Q2 | s2 | Ada was born in Rome. | {state}")
                result = parse_update(f"Q2 | s2 | Ada was born in Rome. | {state}", recover=True)
                self.assertFalse(result.facts)
                self.assertEqual(len(result.rejected_lines), 1)

    def test_recovery_retains_valid_independent_rows(self):
        result = parse_update("Q1 | s1 | Ada directed Fern.\n"
                              "Q2 | s2 | Ada was born in Rome. | DORMANT\n"
                              "Q2 | s2 | Ada was born in Rome.", recover=True)
        self.assertEqual(len(result.facts), 2)
        self.assertEqual(len(result.rejected_lines), 1)

    def test_hints_and_no_evidence_keep_their_existing_meaning(self):
        result = parse_update("NONE\nPLAN_HINT | s1 | Need a query about Ada's school.")
        self.assertFalse(result.facts)
        self.assertEqual(result.hints[0].source_refs, ["s1"])
        self.assertFalse(parse_update("Q1 | NONE | NONE", recover=True).facts)
        missing = parse_update("Q1 | NONE | Ada directed Fern.", recover=True)
        self.assertFalse(missing.facts)
        self.assertTrue(missing.rejected_lines)
        self.assertTrue(parse_update("Q1 | s1 | Ada did not direct Fern.").facts)

    def test_fact_schema_has_no_model_supplied_routing(self):
        with self.assertRaises(ValidationError):
            FactEvent(query_id="Q1", source_refs=["s1"], text="fact", relevance="DORMANT")

    def test_unknown_ids_remain_rejected_after_normalization(self):
        sources = VisibleSources([entry("s1", "Ada directed Fern.", 0).model_dump(mode="json")])
        update = parse_update("Q1 | s1 | valid\nQ9 | s1 | wrong query\nQ1 | future | unread source")
        normalized, _ = sources.normalize_update(update, query_ids={"Q1"}, recover=True)
        self.assertEqual([f.text for f in normalized.facts], ["valid"])
        self.assertEqual(len(normalized.rejected_lines), 2)

    def test_both_prompts_request_three_columns(self):
        prompts = [reading_prompt("q", [], []), reading_repair_prompt(
            "q", [], [], rejected_lines=[], memory="", source_refs=[])]
        for prompt in prompts:
            self.assertIn("three fields", prompt)
            self.assertNotIn("four fields", prompt)
            self.assertNotIn("4. Exactly one action", prompt)
            self.assertNotIn("Mark facts for ACTIVE queries ACTIVE", prompt)


class RuntimeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteEventStore()
        self.addCleanup(self.store.close)
        self.archive = RawArchive(self.store, "routing")
        self.archive.append([entry("s1", "Ada directed Fern.", 0),
                             entry("s2", "Ada was born in Rome.", 1)])
        self.runtime = SubqueryRuntime(run_id="routing", plan=plan(), archive=self.archive, store=self.store)

    def ingest(self, raw, window=0):
        return self.runtime.ingest(parse_update(raw), window_index=window, allowed_refs={"s1", "s2"})

    def bind_parent(self):
        fid = next(iter(self.runtime.state.accepted_ids("Q1")))
        self.runtime.apply_binding(BindingProposal(query_id="Q1", value="Ada", support_refs=[fid]), window_index=0)

    def test_same_window_child_stays_candidate_until_recall(self):
        committed = self.ingest("Q1 | s1 | Ada directed Fern.\nQ2 | s2 | Ada was born in Rome.")
        self.assertEqual(len(committed), 1)
        self.assertEqual(self.runtime.state.executions["Q2"].status, "DORMANT")
        self.assertFalse(self.runtime.state.accepted_ids("Q2"))
        self.bind_parent()
        self.assertEqual(self.runtime.drain_activations(), ["Q2"])
        execution = self.runtime.state.executions["Q2"]
        self.assertTrue(execution.review_pending)
        candidate = self.runtime.candidate_facts("Q2")[0]
        self.runtime.admit_recalled("Q2", {candidate.fact_id}, expected_query_version=execution.version,
                                    expected_binding_version=execution.binding_version)
        self.runtime.finish_review("Q2")
        self.assertIn(candidate.fact_id, self.runtime.state.accepted_ids("Q2"))

    def test_new_fact_after_empty_review_is_accepted(self):
        self.ingest("Q1 | s1 | Ada directed Fern.")
        self.bind_parent()
        self.assertEqual(self.runtime.drain_activations(), ["Q2"])
        self.assertFalse(self.runtime.candidate_facts("Q2"))
        self.runtime.finish_review("Q2")
        committed = self.ingest("Q2 | s2 | Ada was born in Rome.", window=1)
        self.assertEqual(set(committed), self.runtime.state.accepted_ids("Q2"))
        self.assertEqual(len(committed), 1)
        self.assertFalse(self.runtime.candidate_facts("Q2"))
        self.assertEqual(self.runtime.state.executions["Q2"].status, "ACTIVE")
        event = [e for e in self.store.list_runtime_events("routing") if e.event_type == "FACT_COMMITTED"][-1]
        self.assertEqual(event.payload["routing_source"], "QUERY_STATE")
        self.assertEqual(event.payload["query_status"], "ACTIVE")

    def test_resolved_counterevidence_is_accepted_without_automatic_rebinding(self):
        self.ingest("Q1 | s1 | Ada directed Fern.")
        self.bind_parent()
        committed = self.ingest("Q1 | s2 | Bea directed Fern.", window=1)
        self.assertEqual(len(committed), 1)
        self.assertEqual(self.runtime.state.bindings["?director"], "Ada")
        self.assertEqual(self.runtime.state.executions["Q1"].status, "RESOLVED")

    def test_duplicate_dormant_facts_do_not_get_promoted_on_reingestion(self):
        self.ingest("Q2 | s2 | Ada was born in Rome.")
        self.ingest("Q2 | s2 | Ada was born in Rome.", window=1)
        self.assertEqual(len(self.runtime.candidate_facts("Q2")), 1)
        self.assertFalse(self.runtime.state.accepted_ids("Q2"))


class LateFactClient:
    def __init__(self, store):
        self.store, self.updates, self.calls = store, 0, []

    async def complete(self, *, run_id, interface, messages, **kwargs):
        self.calls.append(interface)
        prompt = messages[0]["content"]
        if interface == "UPDATE":
            self.updates += 1
            sources = {p["title"]: p["source_ref"] for p in
                       (json.loads(row[0]) for row in self.store.connection.execute(
                           "select payload_json from raw_archive where run_id=?", (run_id,)))}
            if self.updates == 1:
                return f"Q1 | {sources['Fern']} | Ada directed Fern."
            assert "Q2 [ACTIVE]" in prompt
            return f"Q2 | {sources['Ada']} | Ada was born in Rome."
        if interface == "MEMORY_GROUNDED":
            rows = json.loads(prompt.split("Facts to check (use allowed_context_refs for citations):\n", 1)[1]
                              .split("\n\nOriginal source excerpts:", 1)[0])
            checks = [f"{r['query_id']} | {r['fact_id']} | SUPPORTED | {r['allowed_target_refs'][0]} | faithful" for r in rows]
            bindings = []
            for qid, value, fact in [("Q1", "Ada", "Ada directed Fern."), ("Q2", "Rome", "Ada was born in Rome.")]:
                row = next((r for r in rows if r["query_id"] == qid and r["fact_text"] == fact), None)
                if row and f"{qid} [ACTIVE]" in prompt:
                    bindings.append(f"BIND | {qid} | {value} | {row['fact_id']}")
            return "CHECKS\n" + "\n".join(checks) + "\nBINDINGS\n" + ("\n".join(bindings) or "NONE")
        if interface == "ANSWER":
            assert "Rome" in prompt
            return r"\boxed{Rome}"
        raise AssertionError(f"Unexpected interface: {interface}")


class LateFactIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_review_does_not_strand_fact_from_later_window(self):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = LateFactClient(store)
        sample = CanonicalSample(sample_id="routing", question="Where was the director of Fern born?",
            documents=[CanonicalDocument(document_id="d1", title="Fern", sentences=["Ada directed Fern."]),
                       CanonicalDocument(document_id="d2", title="Ada", sentences=["Ada was born in Rome."])])
        result = await run_subqueries(client=client, config=RunnerConfig(chunk_size=3,
            answer_format="boxed", snapshot_every_windows=0, memory_source_mode="joint_source_memory"),
            sample=sample, plan=plan(), store=store, run_id="routing")
        self.assertEqual(client.updates, 2)
        self.assertEqual(result["state"]["bindings"], {"?director": "Ada", "?birthplace": "Rome"})
        events = store.list_runtime_events("routing")
        empty = next(e for e in events if e.event_type == "DEFER_BUCKET_EMPTY")
        admitted = next(e for e in events if e.event_type == "FACT_COMMITTED" and e.payload["use"]["query_id"] == "Q2")
        self.assertLess(empty.event_seq, admitted.event_seq)
        self.assertNotIn("RECALL", client.calls)
        self.assertNotIn("MEMORY_VERIFY", client.calls)
