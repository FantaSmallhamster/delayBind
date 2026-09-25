"""Single-command utilities for deterministic data preparation and replay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .api import APIConfig, OpenAICompatibleClient
from .data import build_manifest, canonicalize_record, load_records
from .evaluation import ExperimentConfig, run_experiment
from .replay import replay_events
from .runner import RunnerConfig, V5Runner, load_manifest
from .schema import RuntimeEvent, parse_query_plan
from .storage import SQLiteEventStore


def _load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix in {".json", ".jsonl"}:
        return json.loads(text)
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("YAML config requires PyYAML; use JSON or install pyyaml") from exc
    return yaml.safe_load(text) or {}


def _load_env_file(path: str | Path = ".env.local") -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _config_value(config: dict[str, Any], key: str, env_key: str | None = None) -> Any:
    value = config.get(key)
    if value is not None and value != "":
        return value
    return os.environ.get(env_key or key.upper())


def _required_config_value(config: dict[str, Any], key: str, env_key: str) -> str:
    value = _config_value(config, key, env_key)
    if value is None or str(value).strip() == "":
        raise SystemExit(f"run config requires {key!r} or environment variable {env_key}")
    return str(value)


def _api_config(config: dict[str, Any], *, default_seed: int = 4) -> APIConfig:
    merged = {**config, **(config.get("api") or {})}
    model_seed = merged.get("model_seed", default_seed)
    if model_seed is not None:
        model_seed = int(model_seed)
    return APIConfig(
        base_url=_required_config_value(merged, "base_url", "MODEL_BASE_URL"),
        api_key=_required_config_value(merged, "api_key", "MODEL_API_KEY"),
        model=_required_config_value(merged, "model", "MODEL_NAME"),
        temperature=float(merged.get("temperature", 0.0)),
        top_p=float(merged["top_p"]) if merged.get("top_p") is not None else None,
        seed=model_seed,
        timeout_seconds=float(merged.get("timeout_seconds", 120.0)),
        max_retries=int(merged.get("api_max_retries", 2)),
        max_tokens=int(merged.get("max_output_tokens", 4096)),
        enable_thinking=bool(merged.get("enable_thinking", False)),
        connect_ip=merged.get("connect_ip"),
    )


def _with_sentence_splitting(config: dict[str, Any], enabled: bool | None) -> dict[str, Any]:
    if enabled is None:
        return config
    if config.get("runner"):
        return {**config, "runner": {**config["runner"], "sentence_splitting": enabled}}
    return {**config, "sentence_splitting": enabled}


def _run_from_config(config_path: str | Path, *, sentence_splitting: bool | None = None, protocol_version: str | None = None) -> dict[str, Any]:
    _load_env_file()
    config = _with_sentence_splitting(_load_config(config_path), sentence_splitting)
    config = _with_protocol(config, protocol_version)
    dataset_id = str(config.get("dataset_id", "2wiki"))
    if config.get("item") is not None:
        samples = [canonicalize_record(config["item"], dataset_id=dataset_id)]
    elif "question" in config and "context" in config:
        samples = [canonicalize_record(config, dataset_id=dataset_id)]
    else:
        input_path = _required_config_value(config, "input", "DATASET_INPUT")
        samples = list(load_records(input_path, dataset_id=dataset_id))
    if not samples:
        raise SystemExit("dataset has no samples")
    sample_index = int(config.get("sample_index", 0))
    try:
        sample = samples[sample_index]
    except IndexError as exc:
        raise SystemExit(f"sample_index {sample_index} is outside dataset of size {len(samples)}") from exc

    seed = int(config.get("seed", 4))
    order = str(config.get("order", "original"))
    manifest_path = config.get("manifest")
    if manifest_path:
        manifest = load_manifest(manifest_path)
    elif sample.context is not None and config.get("plan_format", "subqueries") == "subqueries":
        if order != "original":
            raise ValueError("raw context already has a fixed order; use original")
        manifest = None
    else:
        manifest = build_manifest(sample, dataset_id=dataset_id, seed=seed, order=order)
        if config.get("manifest_output"):
            Path(config["manifest_output"]).parent.mkdir(parents=True, exist_ok=True)
            manifest.to_json(config["manifest_output"])

    db_path = str(config.get("db", config.get("db_path", ":memory:")))
    store = SQLiteEventStore(db_path)
    api_config = _api_config({**config, "api": {**config.get("api", {}), **config.get("high_api", {})}}, default_seed=seed)
    client = OpenAICompatibleClient(api_config, store=store)
    reader_client = None
    if config.get("low_api"):
        reader_client = OpenAICompatibleClient(_api_config(
            {**config, "api": {**config.get("api", {}), **config["low_api"]}}, default_seed=seed), store=store)
    tokenizer = _load_tokenizer(config.get("tokenizer_path"))
    plan = None
    if config.get("plan") or config.get("plan_path"):
        plan_path = config.get("plan") or config.get("plan_path")
        plan_text = Path(plan_path).read_text(encoding="utf-8")
        if plan_text.lstrip().startswith("{"):
            plan = parse_query_plan(json.loads(plan_text))
        else:
            from .text_protocol_v52 import parse_plan as parse_v3_text_plan
            plan = parse_v3_text_plan(plan_text)
    runner = V5Runner(
        client,
        reader_client=reader_client,
        tokenizer=tokenizer,
        config=RunnerConfig.from_mapping(config.get("runner") or config),
    )
    run_id = str(config.get("run_id") or f"{sample.sample_id}-{manifest.manifest_id if manifest else 'text'}")
    result = asyncio.run(
        runner.run(run_id=run_id, sample=sample, manifest=manifest, store=store, plan=plan)
    )
    output_path = config.get("output", config.get("result_output"))
    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    store.close()
    return result


def _with_protocol(config, version):
    if version is None:
        return config
    if config.get("runner"):
        return {**config, "runner": {**config["runner"], "protocol_version": version}}
    return {**config, "protocol_version": version}


def _load_tokenizer(path: str | None):
    if path is None:
        return None
    if Path(path).suffix == ".json":
        from .tokenization import TokenizerJSON

        return TokenizerJSON(path)
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit("tokenizer_path requires transformers; install the tokenizer extra") from exc
    return AutoTokenizer.from_pretrained(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="delaybind")
    sub = parser.add_subparsers(dest="command", required=True)

    manifest = sub.add_parser("build-manifest")
    manifest.add_argument("--input", required=True)
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--dataset-id", default="2wiki")
    manifest.add_argument("--sample-index", type=int, default=0)
    manifest.add_argument("--seed", type=int, default=4)
    manifest.add_argument(
        "--order",
        choices=["original", "reverse", "interleaved", "distant", "shuffle"],
        default="original",
    )

    replay = sub.add_parser("replay")
    replay.add_argument("--events", required=True)
    replay.add_argument("--output", required=True)

    run = sub.add_parser("run")
    run.add_argument("--config", required=True)

    experiment = sub.add_parser("experiment")
    experiment.add_argument("--config", required=True)
    for command in (run, experiment):
        command.add_argument("--protocol-version", choices=["v5.2-r2"], default=None)
        command.add_argument("--sentence-splitting", action=argparse.BooleanOptionalAction, default=None,
                             help="Enable sentence indexing, or process fact-only text windows")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "build-manifest":
        samples = list(load_records(args.input, dataset_id=args.dataset_id))
        sample = samples[args.sample_index]
        manifest = build_manifest(
            sample,
            dataset_id=args.dataset_id,
            seed=args.seed,
            order=args.order,
        )
        manifest.to_json(args.output)
        return 0
    if args.command == "replay":
        raw = json.loads(Path(args.events).read_text(encoding="utf-8"))
        events = [RuntimeEvent.model_validate(item) for item in raw]
        state = replay_events(events)
        Path(args.output).write_text(json.dumps(state.export(), ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    if args.command == "run":
        result = _run_from_config(args.config, sentence_splitting=args.sentence_splitting, protocol_version=args.protocol_version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "experiment":
        _load_env_file()
        raw_config = _with_sentence_splitting(_load_config(args.config), args.sentence_splitting)
        raw_config = _with_protocol(raw_config, args.protocol_version)
        config = ExperimentConfig.from_mapping(raw_config)
        summary = asyncio.run(
            run_experiment(
                config,
                api_config=_api_config({**raw_config, "api": {**raw_config.get("api", {}), **raw_config.get("high_api", {})}}, default_seed=config.seed),
                reader_api_config=(_api_config({**raw_config, "api": {**raw_config.get("api", {}), **raw_config["low_api"]}}, default_seed=config.seed)
                                   if raw_config.get("low_api") else None),
                tokenizer=_load_tokenizer(config.tokenizer_path),
            )
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
