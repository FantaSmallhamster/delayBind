"""Validate frozen ReMemR1-aligned requests; send them only with --execute.

Credentials are read from MODEL_API_KEY at execution time and never written.
No hidden prompt additions, provider parameters, caching, or answer retries.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import string
import sys
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mwr.freeze_musique_rememr1 import (
    DEFAULT_DEST, TokenizerJSON, canonical, make_payload, selected_function, sha, write_json, write_jsonl,
)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check(condition, message):
    if not condition:
        raise ValueError(message)


async def compare_upstream_requests(bundle, rows, requests, config, tok):
    """Capture requests from the pinned original async function without a network."""
    captured = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {"choices": [{"message": {"content": "offline request comparison"}}]}

    class Session(Response):
        def post(self, **kwargs):
            captured.append(kwargs["json"])
            return Response()

    async def get_session():
        return Session()

    namespace = {"MAX_INPUT_LEN": config["max_input_tokens"],
                 "MAX_OUTPUT_LEN": config["max_output_tokens"],
                 "template_0shot": (bundle / "config/prompt_template.txt").read_text(),
                 "get_async_client": get_session, "URL": "https://offline.invalid/v1", "API_KEY": "offline"}
    function = selected_function(bundle / "provenance/upstream/taskutils/memory_eval/utils/openai_api.py",
                                 "async_query_llm", namespace)
    adapter = SimpleNamespace(encode=tok.encode, decode=lambda ids, **kw: tok.tokenizer.decode(ids, **kw))
    for row, request in zip(rows, requests):
        await function(row, config["model"], adapter, temperature=config["temperature"], top_p=config["top_p"])
        check(captured[-1] == request["json_body"], f"Upstream request differs: {row['id']}")
    check(len(captured) == len(rows), "Upstream did not produce every request")
    return len(captured)


def verify(bundle: Path):
    expected = set()
    for line in (bundle / "SHA256SUMS").read_text().splitlines():
        digest, rel = line.split("  ", 1)
        path = (bundle / rel).resolve()
        check(path.is_relative_to(bundle.resolve()), "Unsafe bundle path")
        check(path.is_file() and sha(path.read_bytes()) == digest, f"Checksum mismatch: {rel}")
        expected.add(rel)
    actual = {str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file()}
    check(actual == expected | {"SHA256SUMS"}, "Unexpected or missing frozen bundle files")
    manifest = json.loads((bundle / "metadata/frozen_manifest.json").read_text())
    for rel, entry in manifest["files"].items():
        check(sha((bundle / rel).read_bytes()) == entry["sha256"], f"Manifest mismatch: {rel}")
    for filename in ["freeze_musique_rememr1.py", "run_frozen_musique_direct.py"]:
        check((ROOT / "scripts/mwr" / filename).read_bytes() ==
              (bundle / "provenance/local_scripts" / filename).read_bytes(),
              f"Runtime script changed since freeze: {filename}")
    config = json.loads((bundle / "config/direct.json").read_text())
    tokenizer_path = ROOT / config["tokenizer_path"]
    check(sha(tokenizer_path.read_bytes()) == config["tokenizer_sha256"], "Tokenizer changed")
    tok = TokenizerJSON(tokenizer_path)
    template = (bundle / "config/prompt_template.txt").read_text()
    check(sha(template.encode()) == config["prompt_template_sha256"], "Template changed")
    rows = json.loads((bundle / "inference/eval_musique_50.json").read_text())
    requests = read_jsonl(bundle / "requests/direct_requests.jsonl")
    ids = json.loads((bundle / "metadata/selected_ids.json").read_text())
    check(len(set(ids)) == len(ids) == len(rows) == len(requests) == manifest["summary"]["sample_count"], "Count mismatch")
    check([r["id"] for r in rows] == [r["sample_id"] for r in requests] == ids, "Sample IDs/order changed")
    truncated = 0
    for row, request in zip(rows, requests):
        check(set(row) == {"id", "type", "input", "context", "num_docs"}, "Labels in inference input")
        headers = re.findall(r"(?m)^Document (\d+):\n", row["context"])
        check(headers == [str(i) for i in range(1, row["num_docs"] + 1)], "Document boundaries changed")
        payload, info = make_payload(row, config, template, tok)
        check(payload == request["json_body"], "Prepared request changed")
        check(sha(canonical(payload)) == request["request_sha256"], "Request hash mismatch")
        truncated += int(info["truncated"])
    compared = asyncio.run(compare_upstream_requests(bundle, rows, requests, config, tok))
    report = {"samples": len(rows), "documents_each": manifest["summary"]["documents_per_question"],
              "checksums_verified": len(expected), "upstream_request_differential_checks_passed": compared,
              "truncated_requests": truncated, "model_calls": 0,
              "manifest_sha256": sha((bundle / "metadata/frozen_manifest.json").read_bytes()),
              "runner_sha256": sha(Path(__file__).read_bytes())}
    return config, requests, report


def upstream_functions(bundle):
    namespace = {"re": re, "string": string, "Counter": Counter,
                 "os": SimpleNamespace(environ={})}  # FORCE_THINK is intentionally unset.
    source = bundle / "provenance/upstream/taskutils/memory_eval/utils/__init__.py"
    for name in ["normalize_answer", "exact_match_score", "f1_score", "sub_exact_match_score",
                 "extract_solution", "extract_answer"]:
        selected_function(source, name, namespace)
    return namespace


def parse_answer(raw_response, functions):
    try:
        content = raw_response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, "INVALID_RESPONSE"
    if not isinstance(content, str) or not content.strip():
        return None, "EMPTY_RESPONSE"
    solution, _ = functions["extract_solution"](content.strip())
    prediction = functions["extract_answer"](solution if solution else content.strip())
    return prediction, "OK" if prediction else "FORMAT_ERROR"


def score_results(bundle, results):
    gold = read_jsonl(bundle / "grading/gold_answers.jsonl")
    by_id = {r["sample_id"]: r for r in results}
    check(len(by_id) == len(results), "Duplicate predictions")
    check(set(by_id) <= {g["sample_id"] for g in gold}, "Unknown prediction IDs")
    functions = upstream_functions(bundle)
    # The official MuSiQue answer metric differs from Hotpot-style yes/no rules.
    official = {"re": re, "string": string, "collections": __import__("collections")}
    metric_source = bundle / "provenance/musique/answer.py"
    for name in ["normalize_answer", "get_tokens", "compute_exact", "compute_f1"]:
        selected_function(metric_source, name, official)
    scored = []
    for row in gold:
        result = by_id.get(row["sample_id"], {})
        prediction = result.get("prediction")
        valid = result.get("status") == "OK" and isinstance(prediction, str) and bool(prediction)
        scored.append({"sample_id": row["sample_id"], "status": result.get("status", "MISSING"),
                       "prediction": prediction,
                       "upstream_primary_em": int(valid and functions["exact_match_score"](prediction, row["answer"])),
                       # Match upstream calc_metrics on one item: round F1 before averaging.
                       "upstream_primary_f1": round(functions["f1_score"](prediction, row["answer"])[0], 2) if valid else 0,
                       "musique_alias_em": max(official["compute_exact"](a, prediction) for a in row["gold_answers"]) if valid else 0,
                       "musique_alias_f1": max(official["compute_f1"](a, prediction) for a in row["gold_answers"]) if valid else 0})
    keys = ["upstream_primary_em", "upstream_primary_f1", "musique_alias_em", "musique_alias_f1"]
    summary = {"denominator": len(gold), "submitted": len(results),
               "status_counts": dict(Counter(r["status"] for r in scored)),
               "percent_scores": {k: round(100 * sum(r[k] for r in scored) / len(gold), 6) for k in keys},
               "failure_policy": "All missing/API/format errors count as zero; unlike upstream skipped empty responses.",
               "support_metrics": "Not measured: this Direct prompt requests an answer only."}
    return scored, summary


def run(bundle, config, requests, preflight, output, transport=None):
    check(not output.exists(), "Output already exists; use a new run directory to prevent mixing repeats")
    key = os.environ.get("MODEL_API_KEY")
    if transport is None:
        check(bool(key), "Set MODEL_API_KEY locally before --execute")
    url = config["base_url"].rstrip("/") + "/chat/completions"

    def http_transport(payload):
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=config["transport_timeout_seconds"]) as response:
            return response.status, json.loads(response.read().decode())

    transport = transport or http_transport
    output.mkdir(parents=True)
    (output / "trajectories").mkdir()
    write_json(output / "preflight.json", preflight)
    write_json(output / "config.resolved.json", config)
    functions = upstream_functions(bundle)
    results = []
    for i, request in enumerate(requests):
        started = time.monotonic()
        trace = {"sample_id": request["sample_id"], "request_sha256": request["request_sha256"],
                 "request": {"url": url, "json_body": request["json_body"]}, "attempt": 1}
        try:
            status, response = transport(request["json_body"])
            trace.update(http_status=status, raw_response=response)
            prediction, outcome = parse_answer(response, functions) if status == 200 else (None, "HTTP_ERROR")
        except HTTPError as exc:
            # Do not persist headers or untrusted error bodies that could echo credentials.
            prediction, outcome = None, "HTTP_ERROR"
            trace.update(http_status=exc.code, error_type=type(exc).__name__)
        except Exception as exc:
            prediction, outcome = None, "TRANSPORT_ERROR"
            trace.update(error_type=type(exc).__name__)
        response_dict = trace.get("raw_response")
        if not isinstance(response_dict, dict):
            response_dict = {}
        choices = response_dict.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        result = {"sample_id": request["sample_id"], "prediction": prediction, "status": outcome,
                  "latency_ms": round((time.monotonic() - started) * 1000, 2),
                  "usage": response_dict.get("usage"),
                  "finish_reason": first_choice.get("finish_reason")}
        trace.update(result)
        # Filenames use sequence numbers, so dataset IDs never become paths.
        write_json(output / "trajectories" / f"{i:04d}.json", trace)
        with (output / "results.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(result, ensure_ascii=False) + "\n")
        results.append(result)
        print(json.dumps({"completed": i + 1, "total": len(requests), "status": outcome}), flush=True)
    scored, summary = score_results(bundle, results)
    write_jsonl(output / "scored.jsonl", scored)
    write_json(output / "scores.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--execute", action="store_true", help="Actually call the frozen model/API")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config, requests, preflight = verify(args.bundle.resolve())
    print(json.dumps(preflight, indent=2))
    if args.execute:
        if args.output is None:
            parser.error("--execute requires a new --output directory")
        print(json.dumps(run(args.bundle.resolve(), config, requests, preflight, args.output.resolve()), indent=2))
