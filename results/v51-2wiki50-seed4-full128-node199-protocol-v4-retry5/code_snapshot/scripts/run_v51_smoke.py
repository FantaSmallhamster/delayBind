"""Run a frozen eight-question V5.1 smoke and score with baseline metrics."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delaybind_core.cli import _api_config, _load_env_file, _load_tokenizer
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness
from delaybind_core.storage import SQLiteEventStore


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def paper_metrics():
    path = ROOT / "taskutils/memory_eval/utils/__init__.py"
    spec = importlib.util.spec_from_file_location("upstream_metrics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(raw: dict, config: ExperimentConfig, tokenizer) -> dict:
    # This path performs all frozen-input checks without requiring a model.
    harness = ExperimentHarness(config, client_factory=lambda store: None)
    samples = harness._samples()
    source = json.loads(Path(config.input).read_text())
    by_id = {str(row["id"]): (index, row) for index, row in enumerate(source)}
    selection = []
    for sample in samples:
        index, record = by_id[sample.sample_id]
        ids = tokenizer.encode(sample.context)
        chunks = [tokenizer.decode(ids[i:i + config.runner.chunk_size])
                  for i in range(0, len(ids), config.runner.chunk_size)]
        if "".join(chunks) != sample.context:
            raise ValueError(f"tokenizer chunk decode changed context for {sample.sample_id}")
        selection.append({
            "sample_id": sample.sample_id, "frozen_index": index,
            "question": sample.question, "question_type": record.get("type"),
            "documents": record["num_docs"], "context_tokens": len(ids),
            "windows": math.ceil(len(ids) / config.runner.chunk_size),
            "context_sha256": hashlib.sha256(sample.context.encode()).hexdigest(),
        })
    baseline = (json.loads(Path(raw["baseline_identity"]).read_text())["identity"]
                if raw.get("baseline_identity") else None)
    if baseline is not None and baseline["input_sha256"] != digest(Path(config.input)):
        raise ValueError("baseline and smoke have different frozen datasets")
    api_reference = (json.loads(Path(raw["api_reference_identity"]).read_text())["identity"]
                     if raw.get("api_reference_identity") else baseline)
    api = raw["api"]
    for field, baseline_field in {
        "model": "model", "base_url": "base_url", "temperature": "temperature",
        "top_p": "top_p", "model_seed": "seed", "max_output_tokens": "max_tokens",
        "enable_thinking": "enable_thinking",
    }.items():
        if api_reference is not None and api[field] != api_reference["api"][baseline_field]:
            raise ValueError(f"baseline API configuration mismatch: {field}")
    files = sorted((ROOT / "delaybind_core").glob("*.py")) + [Path(__file__).resolve(), ROOT / "taskutils/memory_eval/utils/__init__.py"]
    hashes = {str(path.relative_to(ROOT)): digest(path) for path in files}
    return {
        "dataset_sha256": digest(Path(config.input)), "total_samples": len(source),
        "selection_rule": ("configured sample_ids in frozen dataset order" if config.sample_ids else
                           f"frozen dataset rows starting at {config.sample_start}, count={len(samples)}"),
        "selected": selection, "api": api, "data_provenance": raw["data_provenance"],
        "tokenizer_sha256": digest(Path(config.tokenizer_path)),
        "tokenizer_path": config.tokenizer_path, "chunk_size_tokens": config.runner.chunk_size,
        "baseline_identity_path": raw.get("baseline_identity"), "baseline_identity": baseline,
        "api_reference_identity_path": raw.get("api_reference_identity"),
        "baseline_comparison_available": baseline is not None,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "code_hashes": hashes,
        "metrics": "upstream exact_match_score and f1_score against answers[0]; all failures remain in denominator",
        "answer_format_difference": "V5.1 uses ReMemR1 boxed output; historical Direct uses 'the answer is'",
    }


def summarize(raw: dict, audit: dict) -> dict:
    output = Path(raw["output_dir"])
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines() if line.strip()]
    records = {str(row["id"]): row for row in json.loads(Path(raw["input"]).read_text())}
    baseline_rows = json.loads(Path(raw["baseline_results"]).read_text()) if raw.get("baseline_results") else []
    baseline = {row["sample_id"]: row for row in baseline_rows if row["mode"] == "paper" and row["repeat"] == 0}
    metrics = paper_metrics()
    compared = []
    for row in rows:
        original = records[row["sample_id"]]
        prediction = row.get("prediction")
        # Use the original boxed parser, not more permissive alias matching.
        visible = (row.get("raw_answer") or "").split("</think>")[-1].strip()
        parsed = metrics.extract_boxed_answer(visible) if visible else None
        first_gold = original["answers"][0]
        em = bool(parsed is not None and metrics.exact_match_score(parsed, first_gold))
        f1 = metrics.f1_score(parsed, first_gold)[0] if parsed is not None else 0.0
        historical = baseline.get(row["sample_id"])
        if baseline_rows and historical is None:
            raise ValueError(f"baseline has no result for {row['sample_id']}")
        if historical is not None and historical["context_sha256"] != hashlib.sha256(original["context"].encode()).hexdigest():
            raise ValueError("baseline per-question context changed")
        entry = {
            "sample_id": row["sample_id"], "question": original["input"], "gold_first": first_gold,
            "prediction": prediction, "upstream_parsed_answer": parsed,
            "status": row["status"], "runtime_status": row.get("runtime_status"),
            "em": float(em), "f1": f1,
            "baseline_prediction": historical["answer"]["answer"] if historical else None,
            "baseline_em": historical["em"] if historical else None,
            "baseline_f1": historical["f1"] if historical else None,
            "windows": row.get("windows_processed"), "model_calls": row.get("model_calls"),
            "input_tokens": row.get("input_tokens"), "output_tokens": row.get("output_tokens"),
            "deferred_promoted": row.get("deferred_promoted_count", 0),
            "cross_window_promoted": row.get("cross_window_deferred_promoted_count", 0),
            "unresolved_queries": row.get("unresolved_queries", []),
            "error": row.get("error"), "trajectory": row.get("trajectory_path"),
            "error_type": row.get("error_type"),
            "protocol_valid": row.get("protocol_valid"),
            "protocol_repair_failure_count": row.get("protocol_repair_failure_count", 0),
        }
        compared.append(entry)
    compared.sort(key=lambda row: next(item["frozen_index"] for item in audit["selected"] if item["sample_id"] == row["sample_id"]))
    store = SQLiteEventStore(str(output / "experiment.sqlite"))
    calls = [call for row in rows for call in store.list_model_calls(row["run_id"])]
    events = [event for row in rows for event in store.list_runtime_events(row["run_id"])]
    api = raw["api"]
    bad_requests = [call.call_id for call in calls if any([
        call.parameters.get("model") != api["model"], call.parameters.get("temperature") != api["temperature"],
        call.parameters.get("top_p") != api["top_p"], "seed" in call.parameters,
        call.parameters.get("max_tokens") != api["max_output_tokens"],
        call.parameters.get("enable_thinking") != api["enable_thinking"],
    ])]
    if bad_requests:
        raise ValueError("saved API requests do not match the baseline configuration")
    routed_calls = [call for call in calls if call.error is None]
    if api.get("connect_ip") and any(
        (call.raw_response or {}).get("_client_transport", {}).get("peer_ip") != api["connect_ip"]
        for call in routed_calls
    ):
        raise ValueError("a successful request did not use the configured connection IP")
    result = {
        "count": len(compared), "em": sum(row["em"] for row in compared) / len(compared),
        "f1": sum(row["f1"] for row in compared) / len(compared),
        "historical_baseline_em": sum(row["baseline_em"] for row in compared) / len(compared) if baseline_rows else None,
        "historical_baseline_f1": sum(row["baseline_f1"] for row in compared) / len(compared) if baseline_rows else None,
        "baseline_comparison_available": bool(baseline_rows),
        "errors": sum(row["status"] != "OK" for row in compared),
        "missing_final_answers": sum(row["upstream_parsed_answer"] is None for row in compared),
        "parse_failures": sum(row.get("error_type") in {"ProtocolError", "ValidationError", "PlanValidationError"}
                              or row["status"] == "OK" and row["upstream_parsed_answer"] is None for row in compared),
        "protocol_invalid_questions": sum(row.get("protocol_valid") is False for row in compared),
        "protocol_repair_failures": sum(row["protocol_repair_failure_count"] for row in compared),
        "update_rejected_lines": sum(event.event_type == "UPDATE_LINE_REJECTED" for event in events),
        "memory_rejected_lines": sum(event.event_type == "MEMORY_LINE_REJECTED" for event in events),
        "binding_holds": sum(event.event_type == "BINDING_HELD" for event in events),
        "resource_limits": sum(row["runtime_status"] == "RESOURCE_LIMIT" for row in compared),
        "input_tokens": sum(call.input_tokens or 0 for call in calls if not call.cache_hit),
        "output_tokens": sum(call.output_tokens or 0 for call in calls if not call.cache_hit),
        "actual_request_attempts": sum(not call.cache_hit for call in calls),
        "failed_request_attempts": sum(call.error is not None for call in calls),
        "truncated_outputs": sum((call.raw_response or {}).get("choices", [{}])[0].get("finish_reason") == "length" for call in calls),
        "format_repair_events": sum(event.event_type == "AGENT_RESPONSE_INVALID" for event in events),
        "memory_command_rejections": sum(event.event_type == "MEMORY_COMMAND_REJECTED" for event in events),
        "fact_skips": sum(event.event_type == "FACT_SKIPPED" for event in events),
        "cross_window_promotions": sum(row["cross_window_promoted"] for row in compared),
        "request_parameters_audited": True,
        "connect_ip": api.get("connect_ip"),
        "successful_requests_on_configured_ip": len(routed_calls) if api.get("connect_ip") else None,
        "paper_exact_reproduction": False, "rows": compared,
    }
    store.close()
    write_json(output / "baseline_comparison.json", result)
    with (output / "baseline_comparison.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(compared[0]))
        writer.writeheader()
        writer.writerows(compared)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/v51_2wiki50_smoke8.json")
    parser.add_argument("--env-file", default=".env.local")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    raw = json.loads(Path(args.config).read_text())
    config = ExperimentConfig.from_mapping(raw)
    tokenizer = _load_tokenizer(config.tokenizer_path)
    audit = prepare(raw, config, tokenizer)
    output = Path(config.output_dir)
    identity = output / "benchmark.audit.json"
    if identity.exists() and json.loads(identity.read_text()) != audit:
        raise ValueError("frozen benchmark/config/code changed; use a separate output_dir")
    write_json(identity, audit)
    # Keep exactly the source used for this run alongside its hashes.
    for name in audit["code_hashes"]:
        destination = output / "code_snapshot" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / name).read_bytes())
    print(json.dumps({"prepared": len(audit["selected"]), "documents_per_question": config.expected_documents,
                      "total_read_windows": sum(row["windows"] for row in audit["selected"])}, ensure_ascii=False), flush=True)
    if args.prepare_only:
        return
    _load_env_file(args.env_file)
    api_config = _api_config(raw)
    started = time.monotonic()
    harness = ExperimentHarness(config, api_config=api_config, tokenizer=tokenizer)
    asyncio.run(harness.run())
    result = summarize(raw, audit)
    result["wall_time_seconds"] = time.monotonic() - started
    write_json(output / "baseline_comparison.json", result)
    print(json.dumps({key: value for key, value in result.items() if key != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
