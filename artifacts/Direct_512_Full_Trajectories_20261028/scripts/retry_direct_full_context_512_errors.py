"""Retry only failed rows from a completed direct 512-question run."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_direct_full_context_512 import (
    MAX_RETRIES,
    exact_match,
    request_once,
)


async def retry_rows(rows: list[dict], api_key: str) -> list[dict]:
    output = []
    for index, row in enumerate(rows, 1):
        started = time.perf_counter()
        response = None
        prediction = None
        usage = {}
        error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response, usage = await asyncio.to_thread(request_once, row, api_key)
                from scripts.run_direct_full_context_512 import boxed_answer

                prediction = boxed_answer(response)
                error = None
                break
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt < MAX_RETRIES:
                    retry_after = 0.0
                    if isinstance(exc, HTTPError) and exc.headers:
                        try:
                            retry_after = float(exc.headers.get("Retry-After", "0"))
                        except ValueError:
                            pass
                    backoff = min(15 * 2 ** (attempt - 1), 60)
                    await asyncio.sleep(max(retry_after, backoff))
        result = {
            "sample_id": row["id"],
            "question": row["input"],
            "question_type": row["type"],
            "gold_answers": row["answers"],
            "status": "OK" if prediction else "ERROR",
            "prediction": prediction,
            "answer_exact": exact_match(prediction, row["answers"]),
            "error": error,
            "usage": usage,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "raw_response": response,
            "retry_index": index,
            "attempt_policy": "up to 6; 15s exponential backoff capped at 60s, honoring Retry-After",
        }
        output.append(result)
        print(
            f"{datetime.now(timezone.utc).isoformat()} retried={index}/{len(rows)} "
            f"sample={row['id']} status={result['status']} exact={int(result['answer_exact'])}",
            flush=True,
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/test/filtered_seed4_512_quality_v2/eval_2wikimultihopqa_50.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/direct_qwen35_9b_quality512_full_context_t0_r1"),
    )
    args = parser.parse_args()
    input_path = args.input if args.input.is_absolute() else ROOT / args.input
    output = args.output if args.output.is_absolute() else ROOT / args.output
    api_key = os.environ.get("MODEL_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise SystemExit("Set MODEL_API_KEY or SILICONFLOW_API_KEY in the environment")
    data = json.loads(input_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in data}
    first_pass = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    failed = [by_id[row["sample_id"]] for row in first_pass if row["status"] != "OK"]
    if not failed:
        print("No failed rows to retry.")
        return
    retries = asyncio.run(retry_rows(failed, api_key))
    (output / "retry_results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in retries) + "\n",
        encoding="utf-8",
    )
    retry_by_id = {row["sample_id"]: row for row in retries if row["status"] == "OK"}
    merged = []
    for row in first_pass:
        retry = retry_by_id.get(row["sample_id"])
        if retry:
            merged.append({**row, **retry, "status": "OK", "recovered_on_retry": True})
        else:
            merged.append(row)
    (output / "merged_results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in merged) + "\n",
        encoding="utf-8",
    )
    summary = {
        "count": len(merged),
        "correct": sum(row["answer_exact"] for row in merged),
        "accuracy": sum(row["answer_exact"] for row in merged) / len(merged),
        "errors": sum(row["status"] != "OK" for row in merged),
        "retry_attempted": len(retries),
        "retry_recovered": len(retry_by_id),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "note": "Primary results.jsonl is preserved; merged_results.jsonl substitutes only successful retry answers.",
    }
    (output / "final_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print("FINAL", json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
