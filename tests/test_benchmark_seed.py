import random
import re

from scripts.build_2wiki_benchmark import build_records, prepare_source


def source_rows():
    return [{
        "id": f"dev_{i}", "question": f"Question {i}", "golden_answers": [f"Answer {i}"],
        "metadata": {
            "context": {"title": [f"First {i}", f"Second {i}"],
                        "content": [[f"First fact {i}."], [f"Second fact {i}."]]},
            "supporting_facts": {"title": [f"First {i}", f"Second {i}"], "sent_id": [0, 0]},
            "type": "compositional",
        },
    } for i in range(100)]


def test_seed_four_controls_questions_and_distractors_without_global_rng():
    questions, corpus = prepare_source(source_rows())
    first = build_records(questions, corpus, seed=4, sample_count=8, num_docs=50)
    random.seed(9988)
    random.random()
    second = build_records(questions, corpus, seed=4, sample_count=8, num_docs=50)
    assert first == second
    assert [row["index"] for row in first] == [30, 38, 13, 92, 50, 61, 19, 11]
    assert first != build_records(questions, corpus, seed=42, sample_count=8, num_docs=50)
    # Force the same question identity for every eligible row so different
    # context strings measure distractor sampling rather than question choice.
    same_questions = [questions[0]] * len(questions)
    a = build_records(same_questions, corpus, seed=4, sample_count=8, num_docs=50)
    b = build_records(same_questions, corpus, seed=42, sample_count=8, num_docs=50)
    assert a[0]["input"] == b[0]["input"]
    assert a[0]["context"] != b[0]["context"]


def test_fifty_document_generation_preserves_original_support_and_answers():
    source = source_rows()
    questions, corpus = prepare_source(source)
    for row in build_records(questions, corpus, sample_count=8, num_docs=50):
        headers = re.findall(r"(?m)^Document (\d+):$", row["context"])
        assert headers == [str(i) for i in range(1, 51)]
        docs = re.split(r"(?m)^Document \d+:\n", row["context"])[1:]
        record = source[row["index"]]
        assert row["answers"] == record["golden_answers"]
        assert row["input"] == record["question"] + "?"
        for position, title in zip(row["evidence_idx"], record["metadata"]["supporting_facts"]["title"]):
            assert docs[position].startswith(title + "\n")
