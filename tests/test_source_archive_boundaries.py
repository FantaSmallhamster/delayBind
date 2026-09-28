"""Read-prefix and protocol guarantees retained after removing fallback retrieval."""
import unittest

from delaybind_core.archive import FutureSourceAccessError, RawArchive
from delaybind_core.fact_protocol import ProtocolError, parse_memory, parse_selection
from delaybind_core.manifest import ManifestEntry
from delaybind_core.storage import SQLiteEventStore


def entry(ref, position, *, document="bio"):
    return ManifestEntry(source_ref=ref, sample_id="fixture", dataset_id="fixture",
        document_id=document, title=document, sentence_id=position,
        text=f"Ada evidence {ref}.", stream_position=position)


class ArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteEventStore()
        self.addCleanup(self.store.close)
        self.archive = RawArchive(self.store, "fixture", session_prefix=True)

    def test_persisted_but_unobserved_entries_are_not_readable(self):
        self.store.append_raw_span("fixture", entry("future", 0))
        self.archive.append([entry("read", 1)])
        self.assertFalse(self.archive.contains("future"))
        self.assertEqual([x.source_ref for x in self.archive.read_entries()], ["read"])
        for ref in ("missing", "future"):
            with self.subTest(ref=ref), self.assertRaises(FutureSourceAccessError):
                self.archive.entry(ref)
            with self.assertRaises(FutureSourceAccessError):
                self.archive.fetch(ref, neighborhood=1)

    def test_adjacent_source_reread_keeps_read_prefix_and_document_boundary(self):
        self.store.append_raw_span("fixture", entry("future_neighbor", 0))
        self.archive.append([entry("target", 1), entry("read_neighbor", 2), entry("other", 3, document="other")])
        self.assertEqual([e.source_ref for e in self.archive.fetch_sentence_context("target", neighborhood=4)],
                         ["target", "read_neighbor"])
        self.assertNotIn("future_neighbor", [e.source_ref for e in self.archive.fetch("target", neighborhood=4)])

    def test_observing_a_persisted_entry_makes_only_that_entry_available(self):
        self.store.append_raw_span("fixture", entry("now", 0))
        self.store.append_raw_span("fixture", entry("later", 1))
        self.archive.append([entry("now", 0)])
        self.assertTrue(self.archive.contains("now"))
        self.assertFalse(self.archive.contains("later"))
        self.assertEqual([e.source_ref for e in self.archive.fetch("now", neighborhood=1)], ["now"])


class RetainedProtocolTests(unittest.TestCase):
    def test_recall_recovery_keeps_batch_membership(self):
        self.assertEqual(parse_selection("SELECT | F1,F2", {"F1"}, recover=True), {"F1"})
        with self.assertRaises(ProtocolError):
            parse_selection("SELECT | F1,F2", {"F1"})

    def test_memory_only_proposes_existing_bind_and_rebind_actions(self):
        for action in ("DELETE", "KEEP", "PATCH", "RECALL"):
            with self.subTest(action=action), self.assertRaises(ProtocolError):
                parse_memory(f"{action} | Q2 | Rome | F1")
        self.assertTrue(parse_memory("BIND | Q1 | Ada | F1").bindings)
        self.assertTrue(parse_memory("REBIND | Q1 | Bea | F1").rebindings)
