"""Execute immutable Direct requests with a separately recorded scheduler override.

First request gates the batch on endpoint/authentication compatibility. Each
request has one attempt; semantic, transport and HTTP failures are not retried.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mwr.run_frozen_musique_direct import (
    DEFAULT_DEST, parse_answer, score_results, upstream_functions, verify, write_json, write_jsonl,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_json(temporary, value)
    temporary.replace(path)


def run_batch(bundle, output, concurrency, key, transport=None):
    if output.exists():
        raise ValueError("Output exists; refusing to mix repeats")
    config, requests, preflight = verify(bundle)
    print(json.dumps({"preflight": preflight}), flush=True)
    output.mkdir(parents=True)
    (output / "trajectories").mkdir()
    write_json(output / "preflight.json", preflight)
    write_json(output / "config.frozen.json", config)
    started = now()
    execution = {"started_utc": started, "bundle": str(bundle),
                 "manifest_sha256": preflight["manifest_sha256"],
                 "request_body_changes": [], "frozen_concurrency": config["concurrency"],
                 "actual_concurrency": concurrency, "max_attempts_per_question": 1,
                 "repeats": 1, "semantic_retries": 0, "transport_retries": 0,
                 "credential_source": "environment or terminal getpass; never persisted",
                 "executor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(output / "execution.json", execution)
    functions = upstream_functions(bundle)
    url = config["base_url"].rstrip("/") + "/chat/completions"

    def http_transport(payload):
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urlopen(request, timeout=config["transport_timeout_seconds"]) as response:
            return response.status, json.loads(response.read().decode()), {
                "trace_id": response.getheader("x-siliconcloud-trace-id"),
                "request_id": response.getheader("x-request-id")}

    transport = transport or http_transport

    def one(index):
        request = requests[index]
        trace_path = output / "trajectories" / f"{index:04d}.json"
        trace = {"sample_index": index, "sample_id": request["sample_id"],
                 "request_sha256": request["request_sha256"], "attempt": 1,
                 "request": {"url": url, "json_body": request["json_body"]},
                 "started_utc": now(), "status": "IN_FLIGHT"}
        atomic_json(trace_path, trace)
        before = time.monotonic()
        try:
            status, response, metadata = transport(request["json_body"])
            trace.update(http_status=status, raw_response=response, response_metadata=metadata)
            prediction, outcome = parse_answer(response, functions) if status == 200 else (None, "HTTP_ERROR")
        except HTTPError as exc:
            prediction, outcome = None, "HTTP_ERROR"
            trace.update(http_status=exc.code, error_type=type(exc).__name__)
            try:
                message = exc.read().decode(errors="replace")[:4000]
                trace["error_body"] = message.replace(key, "[REDACTED]") if key else message
            except Exception:
                pass
        except Exception as exc:
            prediction, outcome = None, "TRANSPORT_ERROR"
            message = str(exc)
            trace.update(error_type=type(exc).__name__, error_message=message.replace(key, "[REDACTED]") if key else message)
        response = trace.get("raw_response")
        response = response if isinstance(response, dict) else {}
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        result = {"sample_index": index, "sample_id": request["sample_id"], "status": outcome,
                  "prediction": prediction, "latency_ms": round((time.monotonic() - before) * 1000, 2),
                  "usage": response.get("usage"), "finish_reason": choice.get("finish_reason"),
                  "http_status": trace.get("http_status"), "finished_utc": now(),
                  "trajectory_path": str(trace_path.relative_to(output))}
        trace.update(result)
        atomic_json(trace_path, trace)
        return result

    results = []

    def record(result):
        results.append(result)
        with (output / "results.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
        progress = {"completed": len(results), "total": len(requests),
                    "status_counts": dict(Counter(r["status"] for r in results)),
                    "last_sample_id": result["sample_id"], "updated_utc": now()}
        atomic_json(output / "progress.json", progress)
        print(json.dumps(progress, ensure_ascii=False), flush=True)

    first = one(0)
    record(first)
    # Refuse to submit hundreds of doomed requests after a compatibility error.
    aborted = first.get("http_status") != 200
    if not aborted:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            indices = iter(range(1, len(requests)))
            running = {pool.submit(one, i) for i in list(range(1, min(len(requests), concurrency + 1)))}
            for _ in range(len(running)):
                next(indices)
            while running:
                done, running = wait(running, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    record(result)
                    if result.get("http_status") in {401, 403}:
                        aborted = True
                    if not aborted:
                        next_index = next(indices, None)
                        if next_index is not None:
                            running.add(pool.submit(one, next_index))
    results.sort(key=lambda r: r["sample_index"])
    write_jsonl(output / "ordered_results.jsonl", results)
    scored, summary = score_results(bundle, results)
    summary.update(complete=len(results) == len(requests), started_utc=started,
                   finished_utc=now(), aborted=aborted, attempted=len(results))
    write_jsonl(output / "scored.jsonl", scored)
    write_json(output / "scores.json", summary)
    print(json.dumps({"final": summary}, ensure_ascii=False), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps(verify(args.bundle.resolve())[2], indent=2))
    else:
        if not 1 <= args.concurrency <= 32:
            parser.error("concurrency must be 1..32")
        if args.output.exists():
            parser.error("output already exists")
        key = os.environ.get("MODEL_API_KEY") or getpass.getpass("MODEL_API_KEY (hidden): ")
        if not key:
            parser.error("API key is empty")
        run_batch(args.bundle.resolve(), args.output.resolve(), args.concurrency, key)
