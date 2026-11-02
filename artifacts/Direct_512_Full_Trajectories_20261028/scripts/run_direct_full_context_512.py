"""Run the historical relaxed Direct Full Context baseline on a 512-row set."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import string
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PROMPT_TEMPLATE = (
    "Answer the question using the context below. "
    "Give your final answer in \\boxed{}.\n\n"
    "Context:\n{context}\n\nQuestion:\n{question}\n"
)
MODEL = "Qwen/Qwen3.5-9B"
BASE_URL = "https://api.siliconflow.cn/v1"
TEMPERATURE = 0.0
TOP_P = 0.95
SEED = 4
MAX_TOKENS = 4096
MAX_CONCURRENCY = 8
MAX_RETRIES = 6


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize(text: object) -> str:
    text = "".join(c for c in str(text).lower() if c not in string.punctuation)
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", text).split())


def exact_match(prediction: str | None, answers: list[str]) -> bool:
    value = normalize(prediction or "")
    return bool(value) and any(value == normalize(answer) for answer in answers)


def boxed_answer(text: str) -> str:
    start = text.rfind("\\boxed{")
    if start < 0:
        raise ValueError("boxed answer missing")
    start += len("\\boxed{")
    depth = 1
    for index in range(start, len(text)):
        depth += (text[index] == "{") - (text[index] == "}")
        if depth == 0:
            answer = text[start:index].strip()
            if not answer or normalize(answer) in {"answer", "your answer", "final answer"}:
                raise ValueError("empty or placeholder boxed answer")
            return answer
    raise ValueError("unclosed boxed answer")


def prompt(row: dict) -> str:
    return PROMPT_TEMPLATE.replace("{context}", row["context"]).replace("{question}", row["input"])


def request_once(row: dict, api_key: str) -> tuple[str, dict]:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt(row)}],
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "seed": SEED,
        "max_tokens": MAX_TOKENS,
        "enable_thinking": False,
    }
    request = Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urlopen(request, timeout=180) as response:
        raw = json.loads(response.read().decode("utf-8"))
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("response has no choices[0].message.content") from exc
    return content, raw.get("usage", {})


async def main_async(rows: list[dict], output: Path, api_key: str) -> list[dict]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    write_lock = asyncio.Lock()
    completed_path = output / "completed.jsonl"
    done = 0

    async def run_one(index: int, row: dict) -> dict:
        nonlocal done
        started = time.perf_counter()
        raw_response = None
        prediction = None
        error = None
        async with semaphore:
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    raw_response, usage = await asyncio.to_thread(request_once, row, api_key)
                    prediction = boxed_answer(raw_response)
                    break
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if attempt == MAX_RETRIES:
                        break
                    retry_after = 0.0
                    if isinstance(exc, HTTPError) and exc.headers:
                        try:
                            retry_after = float(exc.headers.get("Retry-After", "0"))
                        except ValueError:
                            pass
                    await asyncio.sleep(max(retry_after, min(2 ** (attempt - 1), 60)))
        result = {
            "sample_id": row["id"],
            "question": row["input"],
            "question_type": row["type"],
            "gold_answers": row["answers"],
            "method": "direct_raw_full_context_relaxed_prompt",
            "order": "original",
            "index": index,
            "status": "OK" if prediction else "ERROR",
            "prediction": prediction,
            "answer_exact": exact_match(prediction, row["answers"]),
            "error": error,
            "usage": usage if prediction else {},
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "raw_response": raw_response,
        }
        trace = output / "trajectories" / f"{row['id']}.json"
        trace.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["trajectory_path"] = trace.relative_to(ROOT).as_posix()
        async with write_lock:
            with completed_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            done += 1
            print(
                f"{datetime.now(timezone.utc).isoformat()} completed={done}/{len(rows)} "
                f"sample={row['id']} status={result['status']} exact={int(result['answer_exact'])}",
                flush=True,
            )
        return result

    return await asyncio.gather(*(run_one(index, row) for index, row in enumerate(rows)))


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
    rows = json.loads(input_path.read_text(encoding="utf-8"))
    if len(rows) != 512 or len({row["id"] for row in rows}) != 512:
        raise SystemExit(f"Expected 512 unique questions, found {len(rows)} rows")
    for row in rows:
        if not all(key in row for key in ("id", "input", "context", "answers", "type")):
            raise SystemExit(f"Malformed question row: {row.get('id', '<no id>')}")
    api_key = os.environ.get("MODEL_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise SystemExit("Set MODEL_API_KEY or SILICONFLOW_API_KEY in the environment")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"Output directory is not empty: {output}")
    (output / "trajectories").mkdir(parents=True, exist_ok=True)
    config = {
        "input": input_path.relative_to(ROOT).as_posix() if input_path.is_relative_to(ROOT) else str(input_path),
        "data_sha256": sha256(input_path),
        "model": MODEL,
        "base_url": BASE_URL,
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "seed": SEED,
        "enable_thinking": False,
        "max_tokens": MAX_TOKENS,
        "max_concurrency": MAX_CONCURRENCY,
        "max_retries": MAX_RETRIES,
        "logical_requests_per_question": 1,
        "prompt_template": PROMPT_TEMPLATE,
        "script_sha256": sha256(Path(__file__)),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (output / "config.resolved.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    results = asyncio.run(main_async(rows, output, api_key))
    (output / "results.jsonl").write_text(
        "\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n",
        encoding="utf-8",
    )
    summary = {
        "count": len(results),
        "correct": sum(result["answer_exact"] for result in results),
        "accuracy": sum(result["answer_exact"] for result in results) / len(results),
        "errors": sum(result["status"] != "OK" for result in results),
        "mean_latency_ms": sum(result["latency_ms"] for result in results) / len(results),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print("FINISHED", json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
