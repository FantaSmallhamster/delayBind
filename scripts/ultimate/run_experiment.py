"""Run the normal experiment CLI with in-memory credentials and durable progress.

The wrapper journals completed rows and source fingerprints. It does not change
sampling, inference, retries, scoring, or the experiment's scheduling.
"""

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core import cli
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness
from delaybind_core.agents import LowLevelAgent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected-samples", type=int, default=128, choices=(8, 128))
    args = parser.parse_args()
    raw = json.loads(args.config.read_text())
    config = ExperimentConfig.from_mapping(raw)
    output = Path(config.output_dir)
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output directory is not empty; use a new experiment ID and output directory.")
    # Validate local inputs before reading credentials or making model calls.
    tokenizer = cli._load_tokenizer(config.tokenizer_path)
    assert tokenizer is not None
    samples = ExperimentHarness(config, client_factory=lambda store: None)._samples()
    assert len(samples) == args.expected_samples and len({s.sample_id for s in samples}) == args.expected_samples
    total = len(samples)
    if not os.environ.get("MODEL_API_KEY"):
        os.environ["MODEL_API_KEY"] = getpass.getpass("API key (hidden, memory only): ")
    api = cli._api_config(raw, default_seed=config.seed)
    output.mkdir(parents=True)
    source = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((ROOT / "delaybind_core").glob("*.py"))}
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(), "source_sha256": source,
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "data_sha256": hashlib.sha256(Path(config.input).read_bytes()).hexdigest(),
        "tokenizer_sha256": hashlib.sha256(Path(config.tokenizer_path).read_bytes()).hexdigest(),
        "sample_ids": [s.sample_id for s in samples], "sample_count": len(samples),
        "python": sys.version, "dependencies": {name: importlib.metadata.version(name)
                                                  for name in ("pydantic", "tokenizers")},
        "max_concurrency": config.max_concurrency,
        "mechanisms": {"evidence_rescue": False, "recall_verify": "VERIFY" in LowLevelAgent.interfaces,
                       "raw_archive_fallback": getattr(config.runner, "enable_raw_archive_fallback", False)},
        "instrumentation": "CLI experiment; journal original _run_condition return values only",
    }
    (output / "code_manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    resolved = ExperimentHarness(config, api_config=api)._resolved_config()
    (output / "config.resolved.json").write_text(json.dumps(resolved, indent=2) + "\n")
    original = ExperimentHarness._run_condition
    completed = 0
    def emit(message):
        line = datetime.now(timezone.utc).isoformat() + " " + message
        print(line, flush=True)
        with (output / "run.log").open("a") as log:
            log.write(line + "\n")

    async def journal(self, *condition_args, **condition_kwargs):
        nonlocal completed
        row = await original(self, *condition_args, **condition_kwargs)
        # Runs synchronously in the event loop, preserving each complete line.
        with (output / "completed.jsonl").open("a") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
        completed += 1
        emit(f"completed={completed}/{total} sample={row['sample_id']} status={row['status']} "
             f"exact={int(bool(row.get('answer_exact')))} calls={row.get('model_calls')}")
        return row

    ExperimentHarness._run_condition = journal
    emit(f"START experiment={config.experiment_id} concurrency={config.max_concurrency} samples={total}")
    sys.argv = ["delaybind_core.cli", "experiment", "--config", str(args.config)]
    try:
        status = cli.main()
        emit(f"FINISHED exit_code={status} completed={completed}")
        return status
    finally:
        ExperimentHarness._run_condition = original
        os.environ.pop("MODEL_API_KEY", None)


if __name__ == "__main__":
    raise SystemExit(main())
