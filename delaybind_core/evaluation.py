"""Reproducible multi-method experiment harness for 2Wiki evaluation."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .api import APIConfig, OpenAICompatibleClient
from .data import CanonicalSample, build_manifest, load_records
from .direct import run_direct_full_context
from .metrics import gold_source_refs, plan_relation_recall, score_result, summarize_results
from .oracle import compile_oracle_plans, load_oracle_plans, write_oracle_plans
from .runner import RunnerConfig, V5Runner
from .schema import QueryPlan
from .storage import SQLiteEventStore


SUPPORTED_METHODS = {
    "direct_full_context",
    "v5_predicted",
    "v5_oracle",
    "v5_oracle_no_defer",
    "v5_oracle_flat",
    "v5_oracle_flat_no_defer",
}
SUPPORTED_ORDERS = {"original", "reverse", "interleaved", "distant", "shuffle"}


@dataclass(frozen=True)
class ExperimentConfig:
    input: str
    output_dir: str
    experiment_id: str = "v5-smoke"
    dataset_id: str = "2wiki"
    methods: tuple[str, ...] = (
        "direct_full_context",
        "v5_oracle",
        "v5_predicted",
        "v5_oracle_no_defer",
    )
    orders: tuple[str, ...] = ("original", "reverse")
    sample_start: int = 0
    sample_count: int | None = 16
    sample_ids: tuple[str, ...] = ()
    seed: int = 4
    max_concurrency: int = 1
    resume: bool = True
    retry_errors: bool = True
    fail_fast: bool = False
    oracle_plans: str | None = None
    compile_oracle: bool = False
    runner: RunnerConfig = field(default_factory=RunnerConfig)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ExperimentConfig":
        runner_raw = value.get("runner") or value
        runner = RunnerConfig(
            chunk_size=int(runner_raw.get("chunk_size", 5000)),
            answer_mode=str(runner_raw.get("answer_mode", "runtime")),
            max_windows=int(runner_raw.get("max_windows", 100000)),
            max_verify_candidates=int(runner_raw.get("max_verify_candidates", 1000)),
            max_plan_retries=int(runner_raw.get("max_plan_retries", 1)),
            max_model_calls=int(runner_raw.get("max_model_calls", 1000)),
            snapshot_every_windows=int(runner_raw.get("snapshot_every_windows", 1)),
            verify_committed=bool(runner_raw.get("verify_committed", True)),
            max_graph_claims=int(runner_raw.get("max_graph_claims", 128)),
            defer_unbound=bool(runner_raw.get("defer_unbound", True)),
            max_verify_expansions=int(runner_raw.get("max_verify_expansions", 1)),
            verify_expansion_limit=int(runner_raw.get("verify_expansion_limit", 32)),
            require_evidence_sources=bool(runner_raw.get("require_evidence_sources", True)),
            min_streaming_windows=int(runner_raw.get("min_streaming_windows", 0)),
            query_graph_mode=str(runner_raw.get("query_graph_mode", "open")),
            require_source_span=bool(runner_raw.get("require_source_span", True)),
            callback_retrieval_limit=int(runner_raw.get("callback_retrieval_limit", 16)),
            max_targeted_updates=int(runner_raw.get("max_targeted_updates", 16)),
        )
        methods = tuple(str(item) for item in value.get("methods", cls.methods))
        orders = tuple(str(item) for item in value.get("orders", cls.orders))
        unknown_methods = set(methods) - SUPPORTED_METHODS
        unknown_orders = set(orders) - SUPPORTED_ORDERS
        if unknown_methods:
            raise ValueError(f"unsupported experiment methods: {sorted(unknown_methods)}")
        if unknown_orders:
            raise ValueError(f"unsupported manifest orders: {sorted(unknown_orders)}")
        concurrency = int(value.get("max_concurrency", 1))
        if concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        return cls(
            input=str(value["input"]),
            output_dir=str(value.get("output_dir", "results/v5_experiment")),
            experiment_id=str(value.get("experiment_id", "v5-smoke")),
            dataset_id=str(value.get("dataset_id", "2wiki")),
            methods=methods,
            orders=orders,
            sample_start=int(value.get("sample_start", 0)),
            sample_count=(None if value.get("sample_count") is None else int(value.get("sample_count", 16))),
            sample_ids=tuple(str(item) for item in value.get("sample_ids", [])),
            seed=int(value.get("seed", 4)),
            max_concurrency=concurrency,
            resume=bool(value.get("resume", True)),
            retry_errors=bool(value.get("retry_errors", True)),
            fail_fast=bool(value.get("fail_fast", False)),
            oracle_plans=value.get("oracle_plans"),
            compile_oracle=bool(value.get("compile_oracle", False)),
            runner=runner,
        )


ClientFactory = Callable[[SQLiteEventStore], OpenAICompatibleClient]


def _safe_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")[:80]
    return safe or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _condition_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return str(row.get("sample_id")), str(row.get("method")), str(row.get("order"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


class ExperimentHarness:
    def __init__(
        self,
        config: ExperimentConfig,
        *,
        api_config: APIConfig | None = None,
        client_factory: ClientFactory | None = None,
    ):
        if client_factory is None and api_config is None:
            raise ValueError("api_config or client_factory is required")
        self.config = config
        self.client_factory = client_factory or (
            lambda store: OpenAICompatibleClient(api_config, store=store)  # type: ignore[arg-type]
        )
        self.output_dir = Path(config.output_dir)
        self.db_path = self.output_dir / "experiment.sqlite"
        self.api_metadata = (
            {
                "model": api_config.model,
                "base_url": api_config.base_url,
                "temperature": api_config.temperature,
                "seed": api_config.seed,
                "max_tokens": api_config.max_tokens,
                "enable_thinking": api_config.enable_thinking,
            }
            if api_config is not None
            else {"client_factory": "custom"}
        )

    def _resolved_config(self) -> dict[str, Any]:
        resolved = asdict(self.config)
        resolved["runner"] = asdict(self.config.runner)
        resolved["api"] = self.api_metadata
        semantic = dict(resolved)
        for key in ("output_dir", "resume", "retry_errors", "fail_fast", "max_concurrency"):
            semantic.pop(key, None)
        serialized = json.dumps(semantic, ensure_ascii=False, sort_keys=True)
        resolved["config_fingerprint"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return resolved

    def _samples(self) -> list[CanonicalSample]:
        samples = list(load_records(self.config.input, dataset_id=self.config.dataset_id))
        if self.config.sample_ids:
            wanted = set(self.config.sample_ids)
            samples = [sample for sample in samples if sample.sample_id in wanted]
            missing = wanted - {sample.sample_id for sample in samples}
            if missing:
                raise ValueError(f"sample_ids not found: {sorted(missing)}")
        else:
            end = None if self.config.sample_count is None else self.config.sample_start + self.config.sample_count
            samples = samples[self.config.sample_start:end]
        if not samples:
            raise ValueError("experiment sample selection is empty")
        return samples

    def _oracle_plans(self, samples: list[CanonicalSample]) -> dict[str, QueryPlan]:
        requires_oracle = any("oracle" in method for method in self.config.methods)
        if not requires_oracle:
            return {}
        if self.config.oracle_plans:
            return load_oracle_plans(self.config.oracle_plans)
        if not self.config.compile_oracle:
            raise ValueError(
                "Oracle methods require oracle_plans or compile_oracle=true"
            )
        plans = compile_oracle_plans(samples)
        write_oracle_plans(plans, self.output_dir / "oracle_plans.compiled.json")
        return plans

    async def run(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "trajectories").mkdir(exist_ok=True)
        (self.output_dir / "manifests").mkdir(exist_ok=True)
        resolved = self._resolved_config()
        resolved_path = self.output_dir / "config.resolved.json"
        if self.config.resume and resolved_path.exists():
            previous = json.loads(resolved_path.read_text(encoding="utf-8"))
            if previous.get("config_fingerprint") != resolved["config_fingerprint"]:
                raise ValueError(
                    "resume configuration differs from config.resolved.json; use a new output_dir or resume=false"
                )
        samples = self._samples()
        oracle_plans = self._oracle_plans(samples)
        results_path = self.output_dir / "results.jsonl"
        existing = _read_jsonl(results_path) if self.config.resume else []
        completed = {
            _condition_key(row)
            for row in existing
            if row.get("status") == "OK" or not self.config.retry_errors
        }
        semaphore = asyncio.Semaphore(self.config.max_concurrency)
        jobs = [
            (sample, method, order)
            for sample in samples
            for order in self.config.orders
            for method in self.config.methods
            if (sample.sample_id, method, order) not in completed
        ]

        async def guarded(sample: CanonicalSample, method: str, order: str) -> dict[str, Any]:
            async with semaphore:
                return await self._run_condition(sample, method, order, oracle_plans)

        new_rows: list[dict[str, Any]] = []
        if self.config.fail_fast:
            for job in jobs:
                new_rows.append(await guarded(*job))
        else:
            new_rows = list(await asyncio.gather(*(guarded(*job) for job in jobs)))
        rows_by_key = {_condition_key(row): row for row in [*existing, *new_rows]}
        rows = [rows_by_key[key] for key in sorted(rows_by_key)]
        _write_jsonl(results_path, rows)
        summary = summarize_results(rows)
        (self.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _write_csv(
            self.output_dir / "results.csv",
            rows,
            [
                "sample_id", "method", "order", "runtime_status", "prediction",
                "gold_answers", "answer_exact", "answer_token_f1", "supporting_f1",
                "triple_event_f1", "graph_triple_f1", "verifier_precision", "verifier_recall",
                "verifier_f1", "verifier_micro_precision", "verifier_micro_recall", "verifier_micro_f1",
                "verifier_candidate_count", "plan_valid", "plan_relation_recall",
                "early_latent_evidence_recall", "callback_hit_rate",
                "deferred_to_promoted", "cross_window_deferred_to_promoted",
                "cross_window_deferred_promoted_count", "non_early_deferred_promoted_count",
                "windows_processed", "window_word_budget", "streaming_protocol_valid", "model_calls", "input_tokens", "output_tokens",
                "latency_ms", "error_type", "error",
            ],
        )
        _write_csv(
            self.output_dir / "summary.csv",
            summary["groups"],
            list(summary["groups"][0]) if summary["groups"] else ["method", "order", "count"],
        )
        resolved_path.write_text(
            json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return summary

    async def _run_condition(
        self,
        sample: CanonicalSample,
        method: str,
        order: str,
        oracle_plans: dict[str, QueryPlan],
    ) -> dict[str, Any]:
        manifest = build_manifest(
            sample, dataset_id=self.config.dataset_id, seed=self.config.seed, order=order
        )
        manifest_path = self.output_dir / "manifests" / f"{_safe_id(sample.sample_id)}.{order}.json"
        if not manifest_path.exists():
            manifest.to_json(manifest_path)
        condition = (
            f"{self._resolved_config()['config_fingerprint']}:"
            f"{self.config.experiment_id}:{sample.sample_id}:{method}:{order}"
        )
        run_id = f"{_safe_id(self.config.experiment_id)}-{_safe_id(sample.sample_id)}-{method}-{order}"
        run_id += "-" + hashlib.sha256(condition.encode("utf-8")).hexdigest()[:8]
        store = SQLiteEventStore(str(self.db_path))
        client = self.client_factory(store)
        started = time.perf_counter()
        plan_valid: bool | None = None
        try:
            if method == "direct_full_context":
                result = await run_direct_full_context(
                    run_id=run_id,
                    question=sample.question,
                    manifest=manifest,
                    client=client,
                    store=store,
                )
            else:
                oracle_plan = oracle_plans.get(sample.sample_id)
                if "oracle" in method and oracle_plan is None:
                    raise ValueError(f"missing Oracle Plan for sample {sample.sample_id}")
                plan = oracle_plan if "oracle" in method else None
                if plan is not None:
                    plan_valid = True
                runner_config = replace(
                    self.config.runner,
                    defer_unbound=not method.endswith("no_defer"),
                    query_graph_mode=("flat" if "_flat" in method else self.config.runner.query_graph_mode),
                )
                result = await V5Runner(client, config=runner_config).run(
                    run_id=run_id,
                    sample=sample,
                    manifest=manifest,
                    store=store,
                    plan=plan,
                )
                result["method"] = method
                plan_valid = True
            result.update(
                {
                    "sample_id": sample.sample_id,
                    "question_type": sample.question_type,
                    "method": method,
                    "order": order,
                    "manifest_id": manifest.manifest_id,
                    "plan_valid": plan_valid,
                    "status": "OK",
                }
            )
        except Exception as exc:
            if self.config.fail_fast:
                store.close()
                raise
            result = {
                "run_id": run_id,
                "sample_id": sample.sample_id,
                "question": sample.question,
                "question_type": sample.question_type,
                "method": method,
                "order": order,
                "manifest_id": manifest.manifest_id,
                "plan_valid": False if method == "v5_predicted" else plan_valid,
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "state": {},
                "evidence_pack": None,
            }
        events = [event.model_dump(mode="json") for event in store.list_runtime_events(run_id)]
        calls = [call.model_dump(mode="json") for call in store.list_model_calls(run_id)]
        usage = store.model_call_summary(run_id)
        event_counts = dict(Counter(event["event_type"] for event in events))
        if method == "v5_predicted" and event_counts.get("PLAN_CREATED"):
            result["plan_valid"] = True
        result.update(usage)
        result["latency_ms"] = (time.perf_counter() - started) * 1000.0
        result["event_counts"] = event_counts
        result["events"] = events
        positions = {entry.source_ref: entry.stream_position for entry in manifest.entries}
        committed_positions = [
            positions[event["source_ref"]]
            for event in events
            if event["event_type"] == "CLAIM_COMMITTED"
            and event.get("source_ref") in positions
        ]
        first_grounded = min(committed_positions) if committed_positions else None
        early_gold = {
            source_ref
            for source_ref in gold_source_refs(sample)
            if first_grounded is not None and positions.get(source_ref, first_grounded) < first_grounded
        }
        deferred_refs = {
            event["source_ref"]
            for event in events
            if event["event_type"] == "CLAIM_DEFERRED" and event.get("source_ref")
        }
        result["early_latent_evidence_count"] = len(early_gold)
        result["early_latent_evidence_recall"] = (
            len(early_gold & deferred_refs) / len(early_gold) if early_gold else None
        )
        oracle = oracle_plans.get(sample.sample_id)
        predicted_plan = None
        if (result.get("state") or {}).get("plan"):
            predicted_plan = QueryPlan.model_validate(result["state"]["plan"])
        elif event_counts.get("PLAN_CREATED"):
            plan_event = next(
                event for event in events if event["event_type"] == "PLAN_CREATED"
            )
            predicted_plan = QueryPlan.model_validate(plan_event["payload"]["plan"])
        result["plan_relation_recall"] = plan_relation_recall(predicted_plan, oracle)
        scored = score_result(result, sample)
        trajectory_path = self.output_dir / "trajectories" / f"{run_id}.json"
        trajectory_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "sample_id": sample.sample_id,
                    "method": method,
                    "order": order,
                    "manifest_path": str(manifest_path),
                    "events": events,
                    "model_calls": calls,
                    "state": result.get("state"),
                    "evidence_pack": result.get("evidence_pack"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        scored["trajectory_path"] = str(trajectory_path)
        scored.pop("events", None)
        store.close()
        return scored


async def run_experiment(
    config: ExperimentConfig,
    *,
    api_config: APIConfig | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    return await ExperimentHarness(
        config, api_config=api_config, client_factory=client_factory
    ).run()
