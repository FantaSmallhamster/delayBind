import unittest

from delaybind_core.subquery_runner import post_process_answer


class AnswerNormalizationTests(unittest.TestCase):
    def test_expands_full_film_title_from_question(self):
        question = "Which film came out earlier, Chaplinesque, My Life And Hard Times or Evil Streets?"
        self.assertEqual(
            post_process_answer(question, "Chaplinesque"),
            "Chaplinesque, My Life And Hard Times",
        )

    def test_prefers_modern_name_from_explicit_today_alias(self):
        self.assertEqual(
            post_process_answer(
                "Where was Ferenc Markó's father born?",
                "Lőcse",
                evidence_texts=["Károly Markó was born in Lőcse (today Levoča, Slovakia)."],
            ),
            "Levoča",
        )

    def test_keeps_full_burial_site(self):
        self.assertEqual(
            post_process_answer(
                "Where was the director of My Daughter, the Socialist buried?",
                "Athens",
                evidence_texts=["Alekos Sakellarios is buried in the First Cemetery of Athens in a family grave."],
            ),
            "First Cemetery of Athens",
        )

    def test_reduces_compound_singular_nationality(self):
        self.assertEqual(
            post_process_answer(
                "What is the nationality of the director of 700 Sundays?",
                "American-Canadian",
            ),
            "American",
        )

    def test_extracts_minimal_cause(self):
        self.assertEqual(
            post_process_answer(
                "Why did Zoia Ceaușescu's father die?",
                "he was executed by firing squad after being tried and convicted of economic sabotage",
            ),
            "execution by firing squad",
        )

    def test_does_not_change_place_without_explicit_evidence(self):
        self.assertEqual(
            post_process_answer("Where was the director born?", "United States"),
            "United States",
        )


if __name__ == "__main__":
    unittest.main()
