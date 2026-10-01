"""C-stage source-check parsing and query-local admission contracts."""

import unittest

from delaybind_core.fact_protocol import parse_grounded_memory, parse_source_checks


class SourceCheckParserTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {
            ("Q1", "F1"): {"s1", "s1-neighbor"},
            ("Q2", "F1"): {"s1", "s1-neighbor"},
            ("Q2", "F2"): {"s2"},
        }

    def check(self, raw):
        result = parse_source_checks(raw, self.allowed)
        verdicts = {(c.query_id, c.fact_id): c.verdict for c in result.checks}
        return verdicts, result

    def test_supported_partial_fact_and_source_diff_are_usable_labels(self):
        verdicts, _ = self.check(
            "Q1 | F1 | SUPPORTED | s1 | partial premise\n"
            "Q2 | F1 | SOURCE_DIFF | s1-neighbor | saved wording omitted the school"
        )
        self.assertEqual(verdicts[("Q1", "F1")], "SUPPORTED")
        self.assertEqual(verdicts[("Q2", "F1")], "SOURCE_DIFF")
        self.assertEqual(verdicts[("Q2", "F2")], "UNRESOLVED")

    def test_unknown_pair_and_wrong_fact_source_do_not_spoil_other_checks(self):
        verdicts, result = self.check(
            "Q1 | F1 | SUPPORTED | s2 | wrong fact source\n"
            "Q2 | F2 | SUPPORTED | s2 | faithful\n"
            "Q9 | F1 | SUPPORTED | s1 | invented query"
        )
        self.assertEqual(verdicts[("Q1", "F1")], "UNRESOLVED")
        self.assertEqual(verdicts[("Q2", "F2")], "SUPPORTED")
        self.assertEqual(len(result.rejected_lines), 2)

    def test_supported_without_citation_and_unknown_verdict_fail_closed(self):
        verdicts, result = self.check(
            "Q1 | F1 | SUPPORTED | NONE | no source\n"
            "Q2 | F2 | PASS | s2 | wrong verdict"
        )
        self.assertEqual(set(verdicts.values()), {"UNRESOLVED"})
        self.assertEqual(len(result.rejected_lines), 2)

    def test_conflicting_duplicate_stays_unresolved_after_third_row(self):
        verdicts, _ = self.check(
            "Q1 | F1 | SUPPORTED | s1 | first\n"
            "Q1 | F1 | SOURCE_DIFF | s1 | conflict\n"
            "Q1 | F1 | SUPPORTED | s1 | third"
        )
        self.assertEqual(verdicts[("Q1", "F1")], "UNRESOLVED")

    def test_identical_verdict_and_refs_allow_duplicate_note(self):
        verdicts, result = self.check(
            "Q1 | F1 | SUPPORTED | s1 | note one\n"
            "Q1 | F1 | SUPPORTED | s1 | note two"
        )
        self.assertEqual(verdicts[("Q1", "F1")], "SUPPORTED")
        self.assertFalse(result.rejected_lines)

    def test_same_fact_is_scoped_to_each_query(self):
        verdicts, _ = self.check("Q1 | F1 | SUPPORTED | s1 | faithful")
        self.assertEqual(verdicts[("Q1", "F1")], "SUPPORTED")
        self.assertEqual(verdicts[("Q2", "F1")], "UNRESOLVED")

    def test_binding_command_is_not_a_source_check(self):
        verdicts, result = self.check("BIND | Q1 | Ada | F1")
        self.assertEqual(set(verdicts.values()), {"UNRESOLVED"})
        self.assertTrue(result.rejected_lines)

    def test_grounded_response_requires_both_sections(self):
        result = parse_grounded_memory(
            "CHECKS\nQ1 | F1 | SUPPORTED | s1 | faithful\nBIND | Q1 | Ada | F1",
            self.allowed, {"Q1"},
        )
        self.assertFalse(result.memory.bindings)
        self.assertTrue(result.memory.rejected_lines)

    def test_grounded_bad_check_does_not_discard_independent_query(self):
        result = parse_grounded_memory(
            "CHECKS\n"
            "Q1 | F1 | SUPPORTED | s2 | wrong source\n"
            "Q2 | F2 | SOURCE_DIFF | s2 | corrected by source\n"
            "BINDINGS\n"
            "BIND | Q1 | Ada | F1\n"
            "BIND | Q2 | Larchport | F2",
            self.allowed, {"Q1", "Q2"},
        )
        verdicts = {(c.query_id, c.fact_id): c.verdict for c in result.checks.checks}
        self.assertEqual(verdicts[("Q1", "F1")], "UNRESOLVED")
        self.assertEqual(verdicts[("Q2", "F2")], "SOURCE_DIFF")
        self.assertEqual(len(result.memory.bindings), 2)

    def test_grounded_duplicate_header_fails_closed(self):
        result = parse_grounded_memory(
            "CHECKS\nQ1 | F1 | SUPPORTED | s1 | faithful\n"
            "BINDINGS\nBIND | Q1 | Ada | F1\nBINDINGS\nNONE",
            self.allowed, {"Q1"},
        )
        self.assertFalse(result.memory.bindings)


if __name__ == "__main__":
    unittest.main()
