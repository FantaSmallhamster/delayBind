"""Protect baseline equivalence, source preservation, and scoring denominators."""
import json
import random
from pathlib import Path
import shutil

import pytest

from scripts.mwr.freeze_musique_rememr1 import (
    ROOT, UPSTREAM, TokenizerJSON, TOKENIZER, expand_indices, literal, make_payload, selected_function,
)
from scripts.mwr.run_frozen_musique_direct import (
    compare_upstream_requests, parse_answer, run, score_results, upstream_functions,
)


@pytest.mark.parametrize("original_count,target", [(17, 50), (20, 50), (20, 100), (20, 20)])
def test_expansion_matches_original_source_and_preserves_originals(original_count, target):
    pool = [f"Title {i}\nText {i}" for i in range(150)]
    original = list(range(original_count))
    a, b = random.Random(42), random.Random(42)
    expanded, added, perm = expand_indices(original, len(pool), target, a)
    proxy = type("RandomProxy", (), {"sample": staticmethod(b.sample), "Random": random.Random})
    qa = {"question": "Q?", "outputs": ["A"], "context": original,
          "evidence_idx": [original_count - 1, 0], "type": "2hop", "level": None, "id": "fixture"}
    reference = selected_function(UPSTREAM / "taskutils/data_synthesis/process_test.py",
                                  "generate_input_output", {"QAS": [qa], "DOCS": pool, "random": proxy})(0, target)
    assert reference["context"] == "\n\n".join(f"Document {i+1}:\n{pool[n]}" for i, n in enumerate(expanded))
    assert reference["evidence_idx"] == [perm.index(original_count - 1), perm.index(0)]
    assert a.getstate() == b.getstate()
    assert set(original) <= set(expanded)
    assert not set(original) & set(added)


@pytest.mark.parametrize("original,target,pool", [([1, 2], 1, 10), ([1, 2], 10, 10)])
def test_invalid_expansion_rejected(original, target, pool):
    with pytest.raises(ValueError):
        expand_indices(original, pool, target, random.Random(42))


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "bundle"
    shutil.copytree(UPSTREAM, path / "provenance/upstream")
    (path / "config").mkdir()
    template = literal(UPSTREAM / "taskutils/memory_eval/utils/openai_api.py", "template_0shot")
    (path / "config/prompt_template.txt").write_text(template)
    (path / "provenance/musique").mkdir()
    shutil.copy2(ROOT / "data/raw/musique_v1.0/provenance/metrics/answer.py", path / "provenance/musique/answer.py")
    (path / "grading").mkdir()
    gold = [{"sample_id": "a", "answer": "New York City", "gold_answers": ["New York City", "NYC"]},
            {"sample_id": "b", "answer": "London", "gold_answers": ["London"]}]
    (path / "grading/gold_answers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in gold))
    return path


@pytest.mark.parametrize("limit", [120000, 30])
def test_request_matches_actual_upstream_async_path_including_truncation(bundle, limit):
    import asyncio
    tok = TokenizerJSON(TOKENIZER)
    template = (bundle / "config/prompt_template.txt").read_text()
    config = {"model": "test-model", "temperature": .7, "top_p": .95,
              "max_input_tokens": limit, "max_output_tokens": 10000}
    row = {"id": "a", "context": "Title\n" + "Some real text. " * 70, "input": "Which city?"}
    payload, stats = make_payload(row, config, template, tok)
    assert set(payload) == {"model", "messages", "temperature", "top_p", "max_tokens"}
    assert len(payload["messages"]) == 1 and payload["messages"][0]["role"] == "user"
    assert stats["truncated"] == (limit == 30)
    assert asyncio.run(compare_upstream_requests(bundle, [row], [{"json_body": payload}], config, tok)) == 1


def test_parser_and_missing_denominator_and_aliases(bundle):
    functions = upstream_functions(bundle)
    response = {"choices": [{"message": {"content": "thinking</think>Therefore, the answer is **NYC**."}}]}
    assert parse_answer(response, functions) == ("NYC", "OK")
    assert parse_answer({"choices": [{"message": {"content": "NYC"}}]}, functions) == (None, "FORMAT_ERROR")
    scored, summary = score_results(bundle, [{"sample_id": "a", "prediction": "NYC", "status": "OK"}])
    assert summary["denominator"] == 2
    assert summary["percent_scores"]["upstream_primary_em"] == 0
    assert summary["percent_scores"]["musique_alias_em"] == 50
    assert scored[1]["status"] == "MISSING"
    with pytest.raises(ValueError, match="Duplicate"):
        score_results(bundle, [{"sample_id": "a"}, {"sample_id": "a"}])


def test_runner_sends_frozen_payload_once_logs_raw_response_and_keeps_failure(bundle, tmp_path):
    config = {"base_url": "https://offline.invalid/v1", "transport_timeout_seconds": 1}
    requests = [{"sample_id": sid, "request_sha256": sid, "json_body": {"model": "test", "messages": []}}
                for sid in ["a", "b"]]
    seen = []

    def fake(payload):
        seen.append(payload)
        if len(seen) == 2:
            raise TimeoutError("must not trigger retry")
        return 200, {"choices": [{"message": {"content": "Therefore, the answer is NYC."}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 11, "completion_tokens": 8}}

    out = tmp_path / "run"
    summary = run(bundle, config, requests, {"model_calls": 0}, out, transport=fake)
    assert seen == [r["json_body"] for r in requests]
    trace = json.loads((out / "trajectories/0000.json").read_text())
    assert trace["raw_response"]["usage"]["prompt_tokens"] == 11
    assert summary["denominator"] == 2
    assert summary["status_counts"] == {"OK": 1, "TRANSPORT_ERROR": 1}
    assert "Authorization" not in (out / "trajectories/0000.json").read_text()
