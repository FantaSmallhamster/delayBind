"""End-to-end corrections preserve source gates and invalidate stale descendants."""
import json
import unittest

from delaybind_core.data import CanonicalDocument, CanonicalSample
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema import QueryPlan, Subquery
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.subquery_runner import run_subqueries


class CorrectingClient:
    def __init__(self, store, mode):
        self.store, self.mode = store, mode
        self.windows = 0

    async def complete(self, *, run_id, interface, messages, **kwargs):
        prompt = messages[0]["content"]
        events = self.store.list_runtime_events(run_id)
        facts = {e.payload["fact"]["text"]: e.payload["fact"]
                 for e in events if e.event_type == "FACT_STORED"}
        latest_bindings = {e.payload["query_id"]: e.payload["value"]
                           for e in events if e.event_type == "QUERY_BOUND"}
        if interface == "UPDATE":
            self.windows += 1
            sources = {json.loads(row[0])["text"]: json.loads(row[0])["source_ref"]
                       for row in self.store.connection.execute(
                           "select payload_json from raw_archive where run_id=?", (run_id,))}
            rows = []
            for text, qid in [("Livia's mother is Mira.", "Q1"),
                              ("Mira's mother is Eva.", "Q2"),
                              ("Livia's mother is Nera, not Mira.", "Q1"),
                              ("Nera's mother is Ina.", "Q2")]:
                current = ((self.windows == 1 and text in sources and "Nera" not in text)
                           or (self.windows == 2 and text == "Livia's mother is Nera, not Mira.")
                           or (self.windows == 3 and text == "Nera's mother is Ina."))
                if current and text in sources:
                    rows.append(dict(query=qid, sources=[sources[text]], text=text))
            return json.dumps({"facts": rows, "hints": []})
        if interface == "RECALL":
            lines = prompt.split("Selectable candidate IDs: ", 1)[1].splitlines()[0]
            return lines if latest_bindings.get("Q1") == "Mira" else "NONE"
        if interface == "ANSWER":
            return r"\boxed{Ina}"
        assert interface == "MEMORY_GROUNDED"
        contract = json.loads(prompt.split(
            "Runtime evidence contract (query ID -> allowed action and fact IDs):\n", 1)[1]
            .split("\n\nFacts to check", 1)[0])
        rows = json.loads(prompt.split("Facts to check (use allowed_context_refs for citations):\n", 1)[1]
                          .split("\n\nOriginal source excerpts:", 1)[0])
        checks = [dict(query=r["query_id"], fact=r["fact_id"],
                       verdict="UNRESOLVED" if self.mode == "unsupported" and "not Mira" in r["fact_text"] else "SUPPORTED",
                       sources=[] if self.mode == "unsupported" and "not Mira" in r["fact_text"] else [r["allowed_target_refs"][0]],
                       note="Source check.") for r in rows]
        proposals, replacements = [], []
        def propose(qid, value, text):
            if qid in contract and text in facts:
                proposals.append(dict(query=qid, value=value, support=[facts[text]["fact_id"]]))
        if self.windows == 1:
            if contract.get("Q1", {}).get("action") == "BIND":
                propose("Q1", "Mira", "Livia's mother is Mira.")
            if contract.get("Q2", {}).get("action") == "BIND":
                propose("Q2", "Eva", "Mira's mother is Eva.")
        elif self.windows >= 2:
            if latest_bindings.get("Q1") != "Nera":
                propose("Q1", "Nera", "Livia's mother is Nera, not Mira.")
                if self.mode == "stale_child" and "Q2" in contract:
                    propose("Q2", "STALE_VALUE", "Mira's mother is Eva.")
                if self.mode == "conflict" and proposals:
                    replacements.append(dict(query="Q1", value="Asha", support=proposals[0]["support"]))
            elif self.windows >= 3 and contract.get("Q2", {}).get("action") == "BIND":
                propose("Q2", "Ina", "Nera's mother is Ina.")
        return json.dumps(dict(checks=checks, bindings=proposals, rebindings=replacements))


class CorrectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, mode="normal"):
        store = SQLiteEventStore()
        self.addCleanup(store.close)
        client = CorrectingClient(store, mode)
        sample = CanonicalSample(sample_id="correction", question="Who is Livia's maternal grandmother?",
            documents=[CanonicalDocument(document_id="d1", title="Livia", sentences=["Livia's mother is Mira."]),
                       CanonicalDocument(document_id="d2", title="Mira", sentences=["Mira's mother is Eva."]),
                       CanonicalDocument(document_id="d3", title="Correction", sentences=[
                           "Livia's mother is Nera, not Mira.", "Nera's mother is Ina."])])
        plan = QueryPlan(plan_id="correction", queries=[
            Subquery(id="Q1", template="Who is Livia's mother?", output="?mother"),
            Subquery(id="Q2", template="Who is ?mother's mother?", output="?grandmother", depends_on=["Q1"])])
        result = await run_subqueries(client=client, sample=sample, plan=plan, store=store, run_id="correction",
            config=RunnerConfig(chunk_size=8, answer_format="boxed", snapshot_every_windows=0,
                                memory_source_mode="joint_source_memory", update_protocol="json", memory_protocol="json"))
        return result, store.list_runtime_events("correction")

    async def test_late_bind_proposal_corrects_resolved_parent_and_rebuilds_child(self):
        result, events = await self.run_case()
        self.assertEqual(result["state"]["bindings"], {"?mother": "Nera", "?grandmother": "Ina"})
        self.assertTrue(any(e.event_type == "MEMORY_ACTION_NORMALIZED" and e.payload["effective_action"] == "REBIND" for e in events))
        self.assertTrue(any(e.event_type == "FACT_USE_INVALIDATED" and e.payload["reason"] == "UPSTREAM_BINDING_CHANGED" for e in events))
        self.assertFalse(any(link["value"] == "Eva" for link in result["evidence_pack"]["results"]))

    async def test_normalization_cannot_bypass_unresolved_source_check(self):
        result, events = await self.run_case("unsupported")
        self.assertEqual(result["state"]["bindings"]["?mother"], "Mira")
        self.assertEqual(result["state"]["bindings"]["?grandmother"], "Eva")
        self.assertTrue(any(e.event_type == "MEMORY_SOURCE_SCOPE_REJECTED" and e.payload["reason"] == "SOURCE_CHECK_NOT_ELIGIBLE" for e in events))

    async def test_parent_correction_cannot_apply_old_child_snapshot(self):
        result, events = await self.run_case("stale_child")
        self.assertEqual(result["state"]["bindings"], {"?mother": "Nera", "?grandmother": "Ina"})
        self.assertFalse(any(e.event_type == "QUERY_BOUND" and e.payload["value"] == "STALE_VALUE" for e in events))
        self.assertTrue(any(e.event_type in {"BINDING_HELD", "MEMORY_SOURCE_SCOPE_REJECTED"} for e in events))

    async def test_conflicting_arrays_do_not_select_a_convenient_value(self):
        result, events = await self.run_case("conflict")
        self.assertEqual(result["state"]["bindings"], {"?mother": "Mira", "?grandmother": "Eva"})
        self.assertTrue(any(e.event_type == "BINDING_CONFLICT" and e.payload["reason"] == "MULTIPLE_PROPOSED_VALUES" for e in events))


if __name__ == "__main__":
    unittest.main()
