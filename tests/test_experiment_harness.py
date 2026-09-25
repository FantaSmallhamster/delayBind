"""The current R2 experiment path writes reproducible outputs and resumes."""

import asyncio
import json

import pytest

from delaybind_core.evaluation import ExperimentConfig, run_experiment
from delaybind_core.smoke_r2 import ScriptedR2Client


def test_r2_experiment_writes_artifacts_and_resumes(tmp_path):
    dataset = tmp_path / "fixture.json"
    dataset.write_text(json.dumps([{
        "id": "r2-smoke",
        "question": "Where was Cindy's teacher's mother born?",
        "answer": "Suzhou",
        "context": [
            ["Mary", ["Mary was born in Suzhou."]],
            ["Alice", ["Alice's mother is Mary."]],
            ["Cindy", ["Cindy's teacher is Alice."]],
            ["Review", ["Other Cindy likes Alice."]],
        ],
    }]), encoding="utf-8")
    output = tmp_path / "experiment"
    config = ExperimentConfig.from_mapping({
        "input": str(dataset), "output_dir": str(output), "experiment_id": "fixture",
        "sample_count": 1, "methods": ["v52_r2_predicted"], "orders": ["original"],
        "runner": {"protocol_version": "v5.2-r2", "chunk_size": 32},
    })
    factory = lambda store: ScriptedR2Client()
    summary = asyncio.run(run_experiment(config, client_factory=factory))
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert summary["count"] == 1
    assert len(rows) == 1 and rows[0]["answer_exact"]
    for name in ("results.csv", "summary.csv", "summary.json", "config.resolved.json"):
        assert (output / name).exists()
    assert len(list((output / "trajectories").glob("*.json"))) == 1
    assert len(list((output / "manifests").glob("*.json"))) == 1

    resumed = asyncio.run(run_experiment(config, client_factory=factory))
    assert resumed["count"] == 1
    assert len((output / "results.jsonl").read_text().splitlines()) == 1

    changed = ExperimentConfig.from_mapping({
        "input": str(dataset), "output_dir": str(output), "experiment_id": "fixture",
        "sample_count": 1, "methods": ["v52_r2_no_callback"], "orders": ["original"],
    })
    with pytest.raises(ValueError, match="resume configuration differs"):
        asyncio.run(run_experiment(changed, client_factory=factory))
