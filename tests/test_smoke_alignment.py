import hashlib
import json

import pytest

from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.cli import _api_config
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness


def test_api_preserves_baseline_sampling_without_seed():
    cfg = _api_config({"api": {"base_url": "https://unused", "api_key": "fixture", "model": "fixture",
                               "top_p": 0.95, "temperature": 0.7, "model_seed": None, "max_output_tokens": 10000}})
    payload = OpenAICompatibleClient(cfg)._request_payload("PLAN", [{"role": "user", "content": "test"}])
    assert payload["top_p"] == 0.95 and payload["temperature"] == 0.7
    assert "seed" not in payload and payload["max_tokens"] == 10000


def test_frozen_benchmark_validation_rejects_rebuilt_or_miscounted_inputs(tmp_path):
    data = tmp_path / "data.json"
    records = [{"id": "q1", "input": "question", "context": "Document 1:\nA\nfact", "num_docs": 1, "answers": ["x"]}]
    data.write_text(json.dumps(records))
    values = {"input": str(data), "output_dir": str(tmp_path / "out"), "sample_count": 1,
              "expected_total_samples": 1, "expected_documents": 1,
              "expected_data_sha256": hashlib.sha256(data.read_bytes()).hexdigest()}
    def select(overrides=None):
        return ExperimentHarness(ExperimentConfig.from_mapping({**values, **(overrides or {})}),
                                 client_factory=lambda store: None)._samples()
    assert select()[0].sample_id == "q1"
    with pytest.raises(ValueError, match="SHA-256"):
        select({"expected_data_sha256": "different"})
    with pytest.raises(ValueError, match="sample count"):
        select({"expected_total_samples": 128})
    with pytest.raises(ValueError, match="document count"):
        select({"expected_documents": 50})


def test_json_tokenizer_decodes_windows_like_upstream(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from delaybind_core.cli import _load_tokenizer
    model = tokenizers.Tokenizer(WordLevel({"[UNK]": 0, "first": 1, "second": 2}, unk_token="[UNK]"))
    model.pre_tokenizer = Whitespace()
    path = tmp_path / "tokenizer.json"
    model.save(str(path))
    tokenizer = _load_tokenizer(str(path))
    assert tokenizer.encode("first second") == [1, 2]
    assert tokenizer.decode([1]) == "first"
