"""Current source-first work retains the accepted S4 ANSWER prompt."""
import unittest

from delaybind_core.agent_prompts import final_answer_prompt
from delaybind_core.subquery_runner import parse_final_answer


class AnswerPromptBaselineTests(unittest.TestCase):
    def test_boxed_prompt_preserves_s4_memory_and_source_context(self):
        prompt = final_answer_prompt(
            "Where was Ada born?", "Ada was born in Larchport.", answer_format="boxed",
            raw_context="[TARGET] D1@C0 | Ada | sentence=0 | Ada was born in Larchport.",
        )
        self.assertIn('<ANSWER role=LOW version=v5.1-two-agent-protocol-v4>', prompt)
        self.assertIn('Question:\nWhere was Ada born?', prompt)
        self.assertIn('Working memory:\nAda was born in Larchport.', prompt)
        self.assertIn('Original source recheck (target sentence plus its adjacent sentence units):\n[TARGET] D1@C0 | Ada | sentence=0 | Ada was born in Larchport.', prompt)
        self.assertIn(r'\boxed{answer}', prompt)
        self.assertNotIn('<section>', prompt)
        self.assertNotIn('<update>', prompt)
        self.assertNotIn('<recall>', prompt)
        self.assertIn('For comparison questions', prompt)

    def test_empty_source_context_stays_explicit_and_does_not_add_material(self):
        prompt = final_answer_prompt("Question", "Memory", answer_format="boxed")
        self.assertIn('Original source recheck (target sentence plus its adjacent sentence units):\nNONE', prompt)

    def test_text_and_json_contracts_remain_supported(self):
        text_prompt = final_answer_prompt("Question", "Memory", answer_format="text")
        json_prompt = final_answer_prompt("Question", "Memory", answer_format="json")
        self.assertIn('Return only the short final answer as plain text.', text_prompt)
        self.assertIn('existing AnswerResponse contract', json_prompt)

    def test_existing_boxed_parser_still_accepts_a_valid_response(self):
        self.assertEqual(parse_final_answer(r'Reasoning \boxed{Larchport}', 'boxed').answer, 'Larchport')


if __name__ == '__main__':
    unittest.main()
