import asyncio
import json

import pytest

from delaybind_core.evaluation import ExperimentConfig, run_experiment


class ExperimentClient:
    async def complete(self, *, run_id, interface, messages, response_schema=None, extra=None):
        content = messages[0]["content"]
        if interface == "PLAN":
            return json.dumps(
                {
                    "plan_id": "predicted",
                    "patterns": [
                        {"id": "director", "subject": "Film A", "relation": "director", "object": "?director"}
                    ],
                    "answer_contract": {"target": "?director", "type": "ENTITY"},
                }
            )
        if interface == "UPDATE":
            window = json.loads(content.split("Current window:\n", 1)[1].split("\n\nReturn only", 1)[0])
            entry = window[0]
            return json.dumps(
                {
                    "events": [
                        {
                            "event_id": f"event-{entry['source_ref']}",
                            "source_ref": entry["source_ref"],
                            "subject": "Film A",
                            "concrete_relation": "director",
                            "matched_family": "director",
                            "object": "Martin Lee",
                            "source_order": entry["stream_position"],
                        }
                    ]
                }
            )
        if interface == "VERIFY":
            claim = json.loads(
                content.split("Candidate claim:\n", 1)[1].split("\n\nRaw neighborhood:", 1)[0]
            )
            return json.dumps({"claim_id": claim["claim_id"], "status": "ACCEPT"})
        if interface == "ANSWER":
            context = json.loads(content.split("Complete context:\n", 1)[1].split("\n\nReturn only", 1)[0])
            return json.dumps(
                {
                    "answer": "Martin Lee",
                    "answer_type": "ENTITY",
                    "source_refs": [context[0]["source_ref"]],
                }
            )
        raise AssertionError(interface)


def test_experiment_harness_writes_all_artifacts_and_resumes(tmp_path):
    dataset = tmp_path / "fixture.json"
    dataset.write_text(
        json.dumps(
            [
                {
                    "id": "q1",
                    "type": "compositional",
                    "question": "Who directed Film A?",
                    "answer": "Martin Lee",
                    "evidences": [["Film A", "director", "Martin Lee"]],
                    "supporting_facts": [["Film A", 0]],
                    "context": [["Film A", ["Film A was directed by Martin Lee."]]],
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "experiment"
    config = ExperimentConfig.from_mapping(
        {
            "input": str(dataset),
            "output_dir": str(output),
            "experiment_id": "fixture",
            "sample_count": 1,
            "methods": [
                "direct_full_context",
                "v5_oracle",
                "v5_predicted",
                "v5_oracle_no_defer",
            ],
            "orders": ["original", "reverse"],
            "compile_oracle": True,
            "max_concurrency": 2,
            "runner": {"chunk_size": 10, "max_model_calls": 10},
        }
    )
    factory = lambda store: ExperimentClient()
    summary = asyncio.run(run_experiment(config, client_factory=factory))
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert summary["count"] == 8
    assert len(rows) == 8
    assert all(row["answer_exact"] for row in rows)
    assert (output / "results.csv").exists()
    assert (output / "summary.csv").exists()
    assert (output / "summary.json").exists()
    assert (output / "config.resolved.json").exists()
    assert (output / "oracle_plans.compiled.json").exists()
    assert len(list((output / "trajectories").glob("*.json"))) == 8
    assert len(list((output / "manifests").glob("*.json"))) == 2

    resumed = asyncio.run(run_experiment(config, client_factory=factory))
    assert resumed["count"] == 8
    assert len((output / "results.jsonl").read_text().splitlines()) == 8

    changed = ExperimentConfig.from_mapping(
        {
            "input": str(dataset),
            "output_dir": str(output),
            "experiment_id": "fixture",
            "sample_count": 1,
            "methods": ["direct_full_context"],
            "orders": ["original"],
            "resume": True,
        }
    )
    with pytest.raises(ValueError, match="resume configuration differs"):
        asyncio.run(run_experiment(changed, client_factory=factory))
