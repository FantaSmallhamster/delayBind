"""Legacy helper contracts plus current MEMORY_VERIFY runner regressions.

The helper tests cover historical pure functions, not the active runner protocol.
Runner tests use proposal verdicts; MEMORY_VERIFY never regenerates BIND commands.
"""

import json
import unittest
from types import SimpleNamespace

from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.fact_protocol import parse_memory
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import (
    _memory_recheck_contract, _validate_memory_recheck, run_subqueries,
)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = {
            "Q1": {"action": "BIND", "supporting_fact_ids": ["F111"]},
            "Q2": {"action": "REBIND", "supporting_fact_ids": ["F222"]},
            "Q3": {"action": None, "supporting_fact_ids": []},
        }

    def check(self, text):
        return _validate_memory_recheck(parse_memory(text, recover=True), self.contract)

    def test_valid_bind_and_real_rebind_are_preserved(self):
        result = self.check('BIND | Q1 | Ada | F111\nREBIND | Q2 | Larchport | F222')
        self.assertFalse(result.rejected_lines)
        self.assertEqual(result.bindings[0].value, "Ada")
        self.assertEqual(result.rebindings[0].value, "Larchport")

    def test_unexecuted_bind_cannot_become_rebind(self):
        result = self.check('REBIND | Q1 | Ada | F111')
        self.assertFalse(result.rebindings)
        self.assertIn("requires BIND", result.rejected_lines[0]["reason"])

    def test_resolved_query_requires_rebind(self):
        result = self.check('BIND | Q2 | Larchport | F222')
        self.assertFalse(result.bindings)
        self.assertIn("requires REBIND", result.rejected_lines[0]["reason"])

    def test_source_ids_unknown_ids_and_empty_support_are_rejected(self):
        for ref in ["sample:c00000:D1:s0", "D1@C0", "F999", "[]"]:
            with self.subTest(ref=ref):
                result = self.check(f'BIND | Q1 | Ada | {ref}')
                self.assertFalse(result.bindings)
                self.assertTrue(result.rejected_lines)

    def test_dormant_and_unproposed_queries_are_rejected(self):
        for qid in ["Q3", "Q4"]:
            with self.subTest(query=qid):
                result = self.check(f'BIND | {qid} | Ada | F111')
                self.assertFalse(result.bindings)
                self.assertTrue(result.rejected_lines)

    def test_conflicting_query_is_not_partially_applied(self):
        result = self.check('BIND | Q1 | Ada | F111\nREBIND | Q1 | Bea | F111')
        self.assertFalse(result.bindings or result.rebindings)
        self.assertTrue(result.rejected_lines)

    def test_valid_other_query_survives_semantic_protocol_error(self):
        result = self.check('REBIND | Q1 | Ada | F111\nREBIND | Q2 | Larchport | F222')
        self.assertFalse(result.bindings)
        self.assertEqual([p.query_id for p in result.rebindings], ["Q2"])
        self.assertTrue(result.rejected_lines)

    def test_malformed_output_cannot_bypass_validation(self):
        result = self.check('BIND | Q1 | Ada | F111\nREBIND | Q1')
        self.assertFalse(result.bindings or result.rebindings)
        self.assertTrue(result.rejected_lines)

    def test_none_stays_none_without_forcing_original_proposal(self):
        result = self.check('NONE')
        self.assertFalse(result.bindings or result.rebindings or result.rejected_lines)

    def test_contract_only_offers_visible_accepted_and_reviewed_facts(self):
        state = SimpleNamespace(
            active_fact_ids=["F111", "F222", "F333"],
            facts={fid: SimpleNamespace(source_refs=refs) for fid, refs in {
                "F111": ["s1"], "F222": ["s2"], "F333": ["s1"], "F444": ["s1"],
            }.items()},
            executions={"Q1": SimpleNamespace(status="ACTIVE", review_pending=False, conflicts=[])},
            accepted_ids=lambda qid: {"F111", "F222", "F444"},
        )
        runtime = SimpleNamespace(state=state, dependency_ids=lambda qid: set())
        proposed = parse_memory('REBIND | Q1 | Ada | F111')
        contract = _memory_recheck_contract(proposed, runtime, {"s1"})
        self.assertEqual(contract, {"Q1": {"action": "BIND", "supporting_fact_ids": ["F111"]}})
        self.assertEqual(state.executions["Q1"].status, "ACTIVE")
        state.executions["Q1"].review_pending = True
        self.assertIsNone(_memory_recheck_contract(proposed, runtime, {"s1"})["Q1"]["action"])


class ScriptedClient:
    """Produce the observed protocol failure, then optionally correct it."""
    def __init__(self, store, mode):
        self.store, self.mode = store, mode
        self.rechecks = 0
        self.calls = []
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
            return (f'Q1 | {sources["Fern"]} | Ada directed Fern. | ACTIVE\n'
                    f'Q2 | {sources["Ada"]} | Ada was born in Larchport. | DORMANT')
        if interface == "MEMORY_VERIFY":
            self.rechecks += 1
            if self.mode == "invalid":
                return f'BIND | Q1 | Ada | {director["source_refs"][0]}'
            if self.mode == "none":
                return "NONE"
            qid = "Q1" if self.rechecks == 1 else "Q2"
            verdict = {"contradict": "CONTRADICT", "insufficient": "INSUFFICIENT"}.get(self.mode, "SUPPORT")
            return f"{qid} | {verdict} | original source verdict"
        if interface == "MEMORY":
            if "Q1 [ACTIVE]" in prompt:
                return f'BIND | Q1 | Ada | {director["fact_id"]}'
            if "Q2 [ACTIVE]" in prompt:
                return f'BIND | Q2 | Larchport | {birthplace["fact_id"]}'
            return "NONE"
        if interface == "RECALL":
            return birthplace["fact_id"]
        if interface == "ANSWER":
            self.answer_prompt = prompt
            return r'\boxed{Larchport}'
        raise AssertionError(f"Unexpected interface: {interface}")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, mode):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = ScriptedClient(store, mode)
        sample = CanonicalSample(
            sample_id="protocol_fixture", question="Where was the director of Fern born?",
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

    async def test_old_binding_commands_are_not_verdicts_and_cannot_bind(self):
        result, client, events = await self.run_case("invalid")
        self.assertEqual(result["state"]["bindings"], {})
        self.assertEqual(result["state"]["executions"]["Q2"]["status"], "DORMANT")
        self.assertNotIn("RECALL", client.calls)
        self.assertFalse(any(e.event_type == "QUERY_BOUND" for e in events))
        completed = [e for e in events if e.event_type == "MEMORY_VERIFY_COMPLETED"]
        self.assertTrue(completed)
        self.assertTrue(all(e.payload["verdicts"] == {} for e in completed))
        # The current recovery parser ignores unrecognized rows; it does not
        # retry or turn them into verified proposals.
        self.assertEqual(client.rechecks, 1)

    async def test_contradict_cannot_bind_or_activate_downstream(self):
        result, client, events = await self.run_case("contradict")
        self.assertEqual(result["state"]["bindings"], {})
        self.assertEqual(result["state"]["executions"]["Q2"]["status"], "DORMANT")
        rejected = [e.payload for e in events if e.event_type == "BINDING_REJECTED_BY_SOURCE"]
        self.assertEqual([(e["query_id"], e["reason"]) for e in rejected], [("Q1", "CONTRADICT")])
        self.assertNotIn("RECALL", client.calls)

    async def test_insufficient_does_not_apply_unverified_proposal(self):
        result, client, events = await self.run_case("insufficient")
        self.assertEqual(result["state"]["bindings"], {})
        self.assertEqual(result["state"]["executions"]["Q2"]["status"], "DORMANT")
        self.assertNotIn("RECALL", client.calls)
        self.assertFalse(any(e.event_type == "QUERY_BOUND" for e in events))
        completed = next(e for e in events if e.event_type == "MEMORY_VERIFY_COMPLETED")
        self.assertEqual(completed.payload["verdicts"], {"Q1": "INSUFFICIENT"})

    async def test_semantic_none_is_not_overridden_or_retried(self):
        result, client, events = await self.run_case("none")
        self.assertEqual(client.rechecks, 1)
        self.assertEqual(result["state"]["bindings"], {})
        self.assertTrue(result["protocol_valid"])
        self.assertFalse(any(e.event_type == "QUERY_BOUND" for e in events))

    async def test_support_advances_downstream_without_retry(self):
        result, client, events = await self.run_case("valid")
        self.assertEqual(client.rechecks, 2)
        self.assertEqual(result["state"]["bindings"], {"?director": "Ada", "?birthplace": "Larchport"})
        self.assertEqual(result["unresolved_queries"], [])
        self.assertIn("RECALL", client.calls)
        self.assertNotIn("VERIFY", client.calls)
        self.assertTrue(result["protocol_valid"])
        self.assertFalse(any(e.event_type == "AGENT_RESPONSE_INVALID" for e in events))
        types = [e.event_type for e in events]
        self.assertLess(types.index("MEMORY_VERIFY_COMPLETED"), types.index("QUERY_BOUND"))
        answer_event = next(e for e in events if e.event_type == "RAW_SOURCE_RECHECKED" and e.payload["stage"] == "ANSWER")
        self.assertEqual(len(answer_event.payload["target_refs"]), 2)


if __name__ == "__main__":
    unittest.main()
