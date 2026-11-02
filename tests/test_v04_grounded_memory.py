"""Joint HIGH checks/bindings must preserve C's source and state contracts."""

import json
import unittest

from delaybind_core.agents import LowLevelAgent
from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import run_subqueries


class GroundedClient:
    def __init__(self, store, mode):
        self.store, self.mode = store, mode
        self.calls, self.prompts = [], []

    async def complete(self, *, run_id, interface, messages, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append(interface)
        if interface == "UPDATE":
            sources = {json.loads(row[0])["title"]: json.loads(row[0])["source_ref"]
                       for row in self.store.connection.execute(
                           "select payload_json from raw_archive where run_id=?", (run_id,))}
            return (f"Q1 | {sources['Fern']} | Ada directed Fern.\n"
                    f"Q2 | {sources['Ada']} | Ada was born in Oldport.")
        if interface == "RECALL":
            return prompt.split("Selectable candidate IDs: ", 1)[1].splitlines()[0]
        if interface == "ANSWER":
            return r"\boxed{Larchport}"
        if interface != "MEMORY_GROUNDED":
            raise AssertionError(f"unexpected extra model call: {interface}")
        self.prompts.append(prompt)
        contract = json.loads(prompt.split(
            "Runtime evidence contract (query ID -> allowed action and fact IDs):\n", 1)[1]
            .split("\n\nFacts to check", 1)[0])
        rows = json.loads(prompt.split("Facts to check (use allowed_context_refs for citations):\n", 1)[1]
                          .split("\n\nOriginal source excerpts:", 1)[0])
        checks = []
        for row in rows:
            verdict = "SOURCE_DIFF" if "Oldport" in row["fact_text"] else "SUPPORTED"
            ref = row["allowed_target_refs"][0]
            if self.mode == "unresolved" or (self.mode == "independent" and row["query_id"] == "Q1"):
                verdict = "UNRESOLVED"
            if self.mode == "bad_source":
                ref = "future_source"
            checks.append(f"{row['query_id']} | {row['fact_id']} | {verdict} | {ref} | source assessment")
        bindings = []
        for qid, scope in contract.items():
            if scope["action"] == "REBIND" and self.mode != "rebind":
                continue
            value = "Ada" if qid == "Q1" else "Larchport"
            action = scope["action"]
            if self.mode == "wrong_action":
                action = "REBIND" if action == "BIND" else "BIND"
            if self.mode == "rebind" and qid == "Q1" and action == "BIND":
                value = "Bea"
            refs = ",".join(scope["allowed_fact_ids"])
            if self.mode == "bad_support":
                refs = "F_not_authorized"
            bindings.append(f"{action} | {qid} | {value} | {refs}")
        if self.mode == "partial":
            bindings = []  # Faithful director premise alone cannot answer birthplace.
        if self.mode == "child_in_same_response" and "Q2" not in contract:
            bindings.append(f"BIND | Q2 | ILLEGAL | {rows[0]['fact_id']}")
        if self.mode == "malformed":
            return "BINDINGS\n" + "\n".join(bindings)
        return "CHECKS\n" + "\n".join(checks) + "\nBINDINGS\n" + ("\n".join(bindings) or "NONE")


class GroundedIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, mode="normal", budget=8192):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = GroundedClient(store, mode)
        sample = CanonicalSample(sample_id="joint", question="Where was the director of Fern born?",
            documents=[CanonicalDocument(document_id="d1", title="Fern", sentences=["Ada directed Fern."]),
                       CanonicalDocument(document_id="d2", title="Ada", sentences=["Ada was born in Larchport."])])
        plan = QueryPlan(plan_id="joint", queries=[
            Subquery(id="Q1", template="Who directed Fern?", output="?director"),
            Subquery(id="Q2", template="Where was Ada born?" if mode == "independent" else "Where was ?director born?",
                     output="?birthplace", depends_on=[] if mode == "independent" else ["Q1"]),
        ])
        result = await run_subqueries(client=client, sample=sample, plan=plan, store=store, run_id="joint",
            config=RunnerConfig(answer_format="boxed", snapshot_every_windows=0,
                memory_source_mode="joint_source_memory", memory_source_token_budget=budget))
        return result, client, store.list_runtime_events("joint")

    async def test_single_high_call_and_source_diff_correction(self):
        result, client, events = await self.run_case()
        self.assertEqual(result["state"]["bindings"], {"?director": "Ada", "?birthplace": "Larchport"})
        self.assertEqual(client.calls, ["UPDATE", "MEMORY_GROUNDED", "RECALL", "MEMORY_GROUNDED", "ANSWER"])
        self.assertIn("Oldport", client.prompts[-1])
        self.assertIn("Larchport", client.prompts[-1])
        self.assertTrue(all(e.payload["mode"] == "joint_source_memory" for e in events
                            if e.event_type == "MEMORY_VERIFY_COMPLETED"))

    async def test_unresolved_bad_sources_missing_checks_and_invalid_actions_cannot_commit(self):
        for mode in ("unresolved", "bad_source", "malformed", "wrong_action", "bad_support", "partial"):
            with self.subTest(mode=mode):
                result, _, _ = await self.run_case(mode)
                self.assertEqual(result["state"]["bindings"], {})

    async def test_independent_valid_query_survives_unresolved_pair(self):
        result, _, _ = await self.run_case("independent")
        self.assertEqual(result["state"]["bindings"], {"?birthplace": "Larchport"})

    async def test_parent_binding_cannot_authorize_child_in_same_response(self):
        result, client, events = await self.run_case("child_in_same_response")
        self.assertEqual(result["state"]["bindings"]["?birthplace"], "Larchport")
        self.assertEqual(client.calls.count("MEMORY_GROUNDED"), 2)
        self.assertTrue(any(e.event_type == "MEMORY_LINE_REJECTED" and "ILLEGAL" in e.payload["line"]
                            for e in events))

    async def test_rebinding_parent_invalidates_child_snapshot(self):
        result, _, events = await self.run_case("rebind")
        self.assertEqual(result["state"]["bindings"]["?director"], "Ada")
        self.assertTrue(any(e.event_type == "MEMORY_VERIFY_COMPLETED" for e in events))
        self.assertFalse(any(e.event_type == "MEMORY_SOURCE_SCOPE_REJECTED"
                             and e.payload["reason"] == "QUERY_HAS_NO_READABLE_SOURCE_SCOPE" for e in events))

    async def test_source_budget_blocks_before_grounded_call(self):
        result, client, _ = await self.run_case(budget=1)
        self.assertEqual(result["state"]["status"], "RESOURCE_LIMIT")
        self.assertNotIn("MEMORY_GROUNDED", client.calls)

    async def test_low_cannot_call_grounded_memory(self):
        with self.assertRaises(ValueError):
            await LowLevelAgent(None, None).call("MEMORY_GROUNDED", "")
