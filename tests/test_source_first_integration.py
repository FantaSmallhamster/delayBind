"""B-stage source-first MEMORY integration contracts."""

import json
import unittest

from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import run_subqueries


class SourceFirstClient:
    def __init__(self, store, *, invalid_support=False):
        self.store = store
        self.invalid_support = invalid_support
        self.calls = []
        self.memory_prompts = []
        self.verify_calls = 0

    async def complete(self, *, run_id, interface, messages, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append(interface)
        if interface == "UPDATE":
            rows = self.store.connection.execute(
                "select payload_json from raw_archive where run_id=?", (run_id,)
            )
            refs = {item["title"]: item["source_ref"] for (payload,) in rows
                    for item in [json.loads(payload)]}
            return "\n".join([
                f"Q1 | {refs['Fern']} | Ada directed Fern.",
                f"Q2 | {refs['Ada']} | Ada was born in Larchport.",
            ])
        facts = [event.payload["fact"] for event in self.store.list_runtime_events(run_id)
                 if event.event_type == "FACT_STORED"]
        director = next((fact for fact in facts if fact["text"] == "Ada directed Fern."), None)
        birthplace = next((fact for fact in facts if fact["text"] == "Ada was born in Larchport."), None)
        if interface == "MEMORY":
            self.memory_prompts.append(prompt)
            if "Q1 [ACTIVE]" in prompt:
                support = "F_not_authorized" if self.invalid_support else director["fact_id"]
                return f"BIND | Q1 | Ada | {support}"
            if "Q2 [ACTIVE]" in prompt:
                return f"BIND | Q2 | Larchport | {birthplace['fact_id']}"
            return "NONE"
        if interface == "MEMORY_VERIFY":
            self.verify_calls += 1
            if "mode=SOURCE_PRECHECK" in prompt:
                facts = [event.payload["fact"] for event in self.store.list_runtime_events(run_id)
                         if event.event_type == "FACT_STORED"]
                return "\n".join(
                    f"Q1 | {fact['fact_id']} | SUPPORTED | {fact['source_refs'][0]} | faithful"
                    for fact in facts if fact["text"] == "Ada directed Fern."
                )
            if self.verify_calls == 1:
                return "Q1 | SUPPORT | source supports proposal"
            return "Q2 | SUPPORT | source supports proposal"
        if interface == "RECALL":
            return birthplace["fact_id"]
        if interface == "ANSWER":
            return r"\boxed{Larchport}"
        raise AssertionError(interface)


class SourceFirstIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def sample(self):
        return CanonicalSample(
            sample_id="source_first", question="Where was the director of Fern born?",
            documents=[
                CanonicalDocument(document_id="d1", title="Fern", sentences=["Ada directed Fern."]),
                CanonicalDocument(document_id="d2", title="Ada", sentences=["Ada was born in Larchport."]),
            ],
        )

    def plan(self):
        return QueryPlan(plan_id="source_first", queries=[
            Subquery(id="Q1", template="Who directed Fern?", output="?director"),
            Subquery(id="Q2", template="Where was ?director born?", output="?birthplace", depends_on=["Q1"]),
        ])

    async def run_case(self, *, invalid_support=False, budget=8192):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = SourceFirstClient(store, invalid_support=invalid_support)
        result = await run_subqueries(
            client=client,
            config=RunnerConfig(answer_format="boxed", snapshot_every_windows=0,
                                memory_source_mode="raw_before_memory",
                                memory_source_token_budget=budget),
            run_id="source_first", sample=self.sample(), store=store, plan=self.plan(),
        )
        return result, client, store.list_runtime_events("source_first")

    async def test_memory_gets_scoped_original_source_before_postcheck(self):
        result, client, events = await self.run_case()
        self.assertEqual(result["state"]["bindings"], {"?director": "Ada", "?birthplace": "Larchport"})
        first = client.memory_prompts[0]
        self.assertIn("Runtime evidence contract", first)
        self.assertIn("Original source excerpts", first)
        self.assertIn("[TARGET]", first)
        self.assertIn("Ada directed Fern.", first)
        self.assertNotIn("You cannot read or search the raw archive.", first)
        self.assertLess(client.calls.index("MEMORY"), client.calls.index("MEMORY_VERIFY"))
        completed = [event for event in events if event.event_type == "MEMORY_VERIFY_COMPLETED"]
        self.assertTrue(completed)
        self.assertTrue(all(event.payload["mode"] == "proposal_postcheck" for event in completed))
        self.assertFalse(any(event.event_type == "MEMORY_SOURCE_SCOPE_REJECTED" for event in events))

    async def test_out_of_scope_support_cannot_reach_postcheck_or_write(self):
        result, client, events = await self.run_case(invalid_support=True)
        self.assertEqual(result["state"]["bindings"], {})
        self.assertNotIn("MEMORY_VERIFY", client.calls)
        rejected = [event.payload for event in events if event.event_type == "MEMORY_SOURCE_SCOPE_REJECTED"]
        self.assertEqual(rejected[0]["reason"], "DECLARED_SUPPORT_OUTSIDE_SOURCE_SCOPE")

    async def test_source_context_budget_is_explicit_resource_limit(self):
        result, client, events = await self.run_case(budget=1)
        self.assertEqual(result["state"]["status"], "RESOURCE_LIMIT")
        self.assertIn("SOURCE_CONTEXT_BUDGET", result["state"]["reason_codes"])
        self.assertNotIn("MEMORY", client.calls)
        self.assertTrue(any(event.event_type == "MEMORY_SOURCE_CONTEXT_BUDGET" for event in events))

    async def test_source_precheck_replaces_proposal_postcheck(self):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = SourceFirstClient(store)
        result = await run_subqueries(
            client=client,
            config=RunnerConfig(answer_format="boxed", snapshot_every_windows=0,
                                memory_source_mode="source_verify_before_memory"),
            run_id="source_precheck", sample=self.sample(), store=store, plan=self.plan(),
        )
        events = store.list_runtime_events("source_precheck")
        self.assertTrue(any(event.payload.get("mode") == "source_precheck"
                             for event in events if event.event_type == "MEMORY_VERIFY_COMPLETED"))
        self.assertFalse(any(event.payload.get("mode") == "proposal_postcheck"
                              for event in events if event.event_type == "MEMORY_VERIFY_COMPLETED"))
        self.assertIn("?director", result["state"]["bindings"])


if __name__ == "__main__":
    unittest.main()
