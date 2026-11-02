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
    parser.add_argument("--expected-samples", type=int, default=None)
    args = parser.parse_args()
    if args.expected_samples is not None and args.expected_samples <= 0:
        parser.error("--expected-samples must be a positive integer")
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    config = ExperimentConfig.from_mapping(raw)
    output = Path(config.output_dir)
    if output.exists() and any(output.iterdir()) and not config.resume:
        raise SystemExit("Output directory is not empty; use a new experiment ID and output directory.")
    # Validate local inputs before reading credentials or making model calls.
    tokenizer = cli._load_tokenizer(config.tokenizer_path)
    assert tokenizer is not None
    samples = ExperimentHarness(config, client_factory=lambda store: None)._samples()
    if len({s.sample_id for s in samples}) != len(samples):
        raise ValueError("sample IDs must be unique")
    if args.expected_samples is not None and len(samples) != args.expected_samples:
        raise ValueError(f"expected {args.expected_samples} samples, got {len(samples)}")
    total = len(samples) * len(config.methods) * len(config.orders)
    cli._load_env_file()
    if not os.environ.get("MODEL_API_KEY"):
        os.environ["MODEL_API_KEY"] = getpass.getpass("API key (hidden, memory only): ")
    api = cli._api_config(raw, default_seed=config.seed)
    resolved = ExperimentHarness(config, api_config=api, tokenizer=tokenizer)._resolved_config()
    previous_path = output / "config.resolved.json"
    if config.resume and previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous.get("config_fingerprint") != resolved["config_fingerprint"]:
            raise SystemExit("resume configuration differs; use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    source = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted((ROOT / "delaybind_core").rglob("*.py"))}
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
    (output / "code_manifest.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    # The harness validates the previous fingerprint before writing this file.
    # Overwriting it here used to defeat resume compatibility checks.
    original = ExperimentHarness._run_condition
    expected = {(s.sample_id, method, order) for s in samples for method in config.methods for order in config.orders}
    done = set()
    if config.resume:
        for name in ("results.jsonl", "conditions.completed.jsonl"):
            path = output / name
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip(): continue
                    row = json.loads(line)
                    key = (row.get("sample_id"), row.get("method"), row.get("order"))
                    if key in expected and (row.get("status") in {"OK", "ANSWERED", "INSUFFICIENT", "RESOURCE_LIMIT"} or not config.retry_errors):
                        done.add(key)
    completed = len(done)
    def emit(message):
        line = datetime.now(timezone.utc).isoformat() + " " + message
        print(line, flush=True)
        with (output / "run.log").open("a", encoding="utf-8") as log:
            log.write(line + "\n")

    async def journal(self, *condition_args, **condition_kwargs):
        nonlocal completed
        row = await original(self, *condition_args, **condition_kwargs)
        # Runs synchronously in the event loop, preserving each complete line.
        with (output / "completed.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
        completed += 1
        exact = "NA" if row.get('answer_exact') is None else str(int(bool(row['answer_exact'])))
        emit(f"completed={completed}/{total} sample={row['sample_id']} status={row['status']} "
             f"exact={exact} calls={row.get('model_calls')}")
        return row

    ExperimentHarness._run_condition = journal
    emit(f"START experiment={config.experiment_id} concurrency={config.max_concurrency} samples={len(samples)} conditions={total} resumed={completed}")
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
