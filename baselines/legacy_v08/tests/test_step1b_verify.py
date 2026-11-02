"""Binding verdicts must not blacklist true premises shared across queries."""
import json
import unittest
from pathlib import Path

from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import run_subqueries
from delaybind_core.agent_prompts import memory_prompt, memory_verify_prompt
from delaybind_core.fact_protocol import parse_verification_recover


class ScriptedClient(OpenAICompatibleClient):
    def __init__(self, store, mode="support"):
        self.store = store
        self.mode = mode
        self.calls = []
        self.verify_calls = 0
        self.answer_prompt = ""

    async def complete(self, *, run_id, interface, messages, **kwargs):
        prompt = messages[0]["content"]
        self.calls.append(interface)
        events = self.store.list_runtime_events(run_id)
        facts = [e.payload["fact"] for e in events if e.event_type == "FACT_STORED"]
        director = next((f for f in facts if f["text"] == "Ada directed Fern."), None)
        birthplace = next((f for f in facts if f["text"] == "Ada was born in Larchport."), None)

        if interface == "UPDATE":
            rows = self.store.connection.execute(
                'select payload_json from raw_archive where run_id=?', (run_id,))
            sources = {p["title"]: p["source_ref"] for p in (json.loads(row[0]) for row in rows)}
            return (f'Q1 | {sources["Fern"]} | Ada directed Fern.\n'
                    f'Q2 | {sources["Ada"]} | Ada was born in Larchport.')

        if interface == "MEMORY":
            if "Q1 [ACTIVE]" in prompt:
                return f'BIND | Q1 | Ada | {director["fact_id"]}'
            if "Q2 [ACTIVE]" in prompt:
                if self.mode == "reject_rebind_q1":
                    return (f'REBIND | Q1 | Bea | {director["fact_id"]}\n'
                            f'BIND | Q2 | Larchport | {birthplace["fact_id"]}')
                return f'BIND | Q2 | Larchport | {birthplace["fact_id"]}'
            return "NONE"

        if interface == "MEMORY_VERIFY":
            self.verify_calls += 1
            proposed = set()
            if "Proposals to verify" in prompt:
                section = prompt.split("Proposals to verify", 1)[1].split("Original source recheck:", 1)[0]
                import re as _re
                proposed = set(_re.findall(r"Q\d+", section))
            verdicts = []
            if "Q1" in proposed:
                if self.mode == "reject_rebind_q1":
                    verdicts.append("Q1 | SUPPORT | source confirms" if self.verify_calls == 1
                                    else "Q1 | CONTRADICT | source states Ada, not Bea")
                elif self.mode in ("support", "contradict_q2"):
                    verdicts.append("Q1 | SUPPORT | source confirms")
                elif self.mode == "contradict_q1":
                    verdicts.append("Q1 | CONTRADICT | source says otherwise")
                elif self.mode == "insufficient_q1":
                    verdicts.append("Q1 | INSUFFICIENT | no evidence")
            if "Q2" in proposed:
                if self.mode in ("support", "reject_rebind_q1"):
                    verdicts.append("Q2 | SUPPORT | source confirms")
                elif self.mode == "contradict_q2":
                    verdicts.append("Q2 | CONTRADICT | source says otherwise")
            return "\n".join(verdicts) if verdicts else "NONE"

        if interface == "RECALL":
            return birthplace["fact_id"]


        if interface == "ANSWER":
            self.answer_prompt = prompt
            return r'\boxed{Larchport}'

        raise AssertionError(f"Unexpected interface: {interface}")


class Step1bTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, mode):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = ScriptedClient(store, mode)
        sample = CanonicalSample(
            sample_id="step1b_fixture", question="Where was the director of Fern born?",
            documents=[
                CanonicalDocument(document_id="d1", title="Fern", sentences=["Ada directed Fern."]),
                CanonicalDocument(document_id="d2", title="Ada", sentences=["Ada was born in Larchport."]),
            ],
        )
        plan = QueryPlan(plan_id="fixture", queries=[
            Subquery(id="Q1", template="Who directed Fern?", output="?director"),
            Subquery(id="Q2", template="Where was ?director born?", output="?birthplace", depends_on=["Q1"]),
        ])
        result = await run_subqueries(
            client=client, config=RunnerConfig(answer_format="boxed", snapshot_every_windows=0),
            run_id="fixture", sample=sample, store=store, plan=plan,
        )
        return result, client, store.list_runtime_events("fixture")

    async def test_support_executes_binding(self):
        """MEMORY_VERIFY SUPPORT → original BIND proposal is executed."""
        result, client, events = await self.run_case("support")
        self.assertEqual(result["state"]["bindings"], {"?director": "Ada", "?birthplace": "Larchport"})
        self.assertEqual(result["unresolved_queries"], [])
        self.assertIn("MEMORY_VERIFY", client.calls)
        self.assertGreater(client.verify_calls, 0)
        types = [e.event_type for e in events]
        self.assertIn("MEMORY_VERIFY_COMPLETED", types)
        self.assertNotIn("BINDING_REJECTED_BY_SOURCE", types)

    async def test_contradict_blocks_binding(self):
        """CONTRADICT blocks the proposal, without rejecting its premises."""
        result, client, events = await self.run_case("contradict_q1")
        # Q1 not bound because contradicted
        self.assertNotIn("?director", result["state"]["bindings"])
        types = [e.event_type for e in events]
        self.assertIn("BINDING_REJECTED_BY_SOURCE", types)
        self.assertNotIn("FACT_REJECTED_BY_RECHECK", types)
        reject_events = [e for e in events if e.event_type == "BINDING_REJECTED_BY_SOURCE"]
        self.assertEqual(reject_events[0].payload["query_id"], "Q1")
        self.assertEqual(reject_events[0].payload["reason"], "CONTRADICT")

    async def test_insufficient_blocks_binding(self):
        """MEMORY_VERIFY INSUFFICIENT → BIND not executed."""
        result, client, events = await self.run_case("insufficient_q1")
        self.assertNotIn("?director", result["state"]["bindings"])
        types = [e.event_type for e in events]
        self.assertNotIn("FACT_REJECTED_BY_RECHECK", types)  # INSUFFICIENT != CONTRADICT

    async def test_rejected_binding_does_not_blacklist_accepted_premises(self):
        result, client, events = await self.run_case("contradict_q2")
        # Q1 bound, Q2 contradicted
        self.assertIn("?director", result["state"]["bindings"])
        types = [e.event_type for e in events]
        reject_events = [e for e in events if e.event_type == "BINDING_REJECTED_BY_SOURCE"]
        self.assertTrue(any(e.payload["query_id"] == "Q2" for e in reject_events))
        self.assertNotIn("?birthplace", result["state"]["bindings"])
        self.assertIn("Ada was born in Larchport.", client.answer_prompt)
        self.assertNotIn("FACT_REJECTED_BY_RECHECK", types)

    async def test_rejected_rebind_keeps_previous_binding_and_its_evidence(self):
        result, client, events = await self.run_case("reject_rebind_q1")
        self.assertEqual(result["state"]["bindings"]["?director"], "Ada")
        self.assertEqual(result["state"]["bindings"]["?birthplace"], "Larchport")
        self.assertIn("Ada directed Fern.", client.answer_prompt)
        raw = next(e for e in events if e.event_type == "RAW_SOURCE_RECHECKED" and e.payload["stage"] == "ANSWER")
        self.assertEqual(len(raw.payload["target_refs"]), 2)
        rejected = [e.payload for e in events if e.event_type == "BINDING_REJECTED_BY_SOURCE"]
        self.assertEqual([(r["action"], r["query_id"], r["value"]) for r in rejected], [("REBIND", "Q1", "Bea")])

    async def test_verify_only_judges_does_not_regenerate(self):
        """MEMORY_VERIFY prompt contains proposals but asks only for verdicts."""
        result, client, events = await self.run_case("support")
        # The verify call should have happened
        self.assertGreater(client.verify_calls, 0)
        # No MEMORY_RECHECK events (old flow)
        types = [e.event_type for e in events]
        self.assertNotIn("MEMORY_RECHECK_STARTED", types)
        self.assertNotIn("MEMORY_RECHECK_COMPLETED", types)


class ComparisonClient:
    """Reproduce correct dates being discarded after a wrong comparison."""
    def __init__(self, store):
        self.store = store
        self.verify_calls = 0
        self.answer_prompt = ""

    async def complete(self, *, run_id, interface, messages, **kwargs):
        prompt = messages[0]["content"]
        if interface == "UPDATE":
            row = self.store.connection.execute('select source_ref from raw_archive where run_id=?', (run_id,)).fetchone()
            return (f'Q1 | {row[0]} | Ada was born in 1961.\n'
                    f'Q2 | {row[0]} | Bea was born in 1934.')
        facts = [e.payload["fact"] for e in self.store.list_runtime_events(run_id) if e.event_type == "FACT_STORED"]
        ada = next(f["fact_id"] for f in facts if f["text"] == "Ada was born in 1961.")
        bea = next(f["fact_id"] for f in facts if f["text"] == "Bea was born in 1934.")
        if interface == "MEMORY":
            if "Q1 [ACTIVE]" in prompt:
                return (f'BIND | Q1 | 1961 | {ada}\nBIND | Q2 | 1934 | {bea}\n'
                        f'BIND | Q3 | Ada | {ada},{bea}')
            if "Q3 [ACTIVE]" in prompt:
                return f'BIND | Q3 | Bea | {ada},{bea}'
            return "NONE"
        if interface == "MEMORY_VERIFY":
            self.verify_calls += 1
            if self.verify_calls == 1:
                return 'Q1 | SUPPORT | 1961\nQ2 | SUPPORT | 1934\nQ3 | CONTRADICT | 1934 is earlier than 1961'
            return 'Q3 | SUPPORT | Bea was born earlier'
        if interface == "ANSWER":
            self.answer_prompt = prompt
            return r'\boxed{Bea}'
        raise AssertionError(interface)


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_conclusion_does_not_remove_correct_dates_from_answer(self):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = ComparisonClient(store)
        result = await run_subqueries(
            client=client, config=RunnerConfig(answer_format="boxed", snapshot_every_windows=0),
            run_id="comparison", store=store,
            sample=CanonicalSample(sample_id="comparison", question="Who was born earlier, Ada or Bea?",
                documents=[CanonicalDocument(document_id="d1", title="Dates", sentences=["Ada was born in 1961. Bea was born in 1934."])]),
            plan=QueryPlan(plan_id="comparison", queries=[
                Subquery(id="Q1", template="When was Ada born?", output="?ada_date"),
                Subquery(id="Q2", template="When was Bea born?", output="?bea_date"),
                Subquery(id="Q3", template="Who was born earlier given ?ada_date and ?bea_date?", output="?earlier", depends_on=["Q1", "Q2"]),
            ]),
        )
        self.assertEqual(result["state"]["bindings"]["?earlier"], "Bea")
        self.assertEqual(client.verify_calls, 2)
        self.assertIn("Ada was born in 1961.", client.answer_prompt)
        self.assertIn("Bea was born in 1934.", client.answer_prompt)
        events = store.list_runtime_events("comparison")
        self.assertFalse(any(e.event_type == "FACT_REJECTED_BY_RECHECK" for e in events))
        rejected = [e.payload for e in events if e.event_type == "BINDING_REJECTED_BY_SOURCE"]
        self.assertEqual([(r["query_id"], r["value"]) for r in rejected], [("Q3", "Ada")])
        self.assertTrue(any(e.event_type == "RAW_SOURCE_RECHECKED" and e.payload["stage"] == "ANSWER" and e.payload["target_refs"] for e in events))


class HintTests(unittest.TestCase):
    def test_memory_verify_recovery_keeps_explicit_rows_and_ignores_prose(self):
        raw = ("The earlier paragraph was not a verdict.\n"
               "Q1 | SUPPORT | source confirms the relation.\n"
               "Re-evaluation of Q2: the source text is insufficient.")
        self.assertEqual(parse_verification_recover(raw, {"Q1", "Q2"}),
                         {"Q1": "SUPPORT"})

    def test_memory_accepts_runtime_dictionary_hints(self):
        prompt = memory_prompt("question", [], "NONE", hints=[{"text": "a hint", "source_refs": ["s0"]}], eof=False)
        self.assertIn('"text": "a hint"', prompt)

    def test_verify_accepts_runtime_dictionary_hints(self):
        prompt = memory_verify_prompt("question", "", "NONE", [], "NONE", hints=[{"text": "a hint", "source_refs": ["s0"]}])
        self.assertIn('"source_refs": ["s0"]', prompt)


if __name__ == "__main__":
    unittest.main()
