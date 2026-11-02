"""Package the existing Direct 512 run without model calls or altering its files.

Only the request payload is reconstructed. Saved response text and result rows
are copied verbatim; unrecorded HTTP responses and attempts remain unavailable.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import string
import zipfile


ROOT = Path(__file__).resolve().parents[2]
RUN_NAME = "direct_qwen35_9b_quality512_full_context_t0_r1"
BUNDLE = ROOT / "data/test/2wiki_512_complete_eval_bundle_20261026"
SECRET_PATTERN = re.compile(rb"sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9_.-]{20,}")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def primary_exact(prediction: str | None, answers: list[str]) -> bool:
    def normalize(value: str) -> str:
        return " ".join("".join(c for c in value.lower() if c not in string.punctuation).split())
    value = normalize(prediction or "")
    return bool(value) and any(value == normalize(answer) for answer in answers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_root.resolve()
    run = source / "results" / RUN_NAME
    output = args.output_dir.resolve()
    archive = output.with_suffix(".zip")
    require(not output.exists() and not archive.exists(), "Choose an unused output path")

    config = json.loads((run / "config.resolved.json").read_text())
    dataset_path = source / config["input"]
    script_path = source / "scripts/run_direct_full_context_512.py"
    require(sha(dataset_path.read_bytes()) == config["data_sha256"], "Original dataset hash changed")
    require(sha(script_path.read_bytes()) == config["script_sha256"], "Original runner hash changed")
    # Read literal constants, never execute the historical network runner.
    constants = {}
    for node in ast.parse(script_path.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    for field, constant in {
        "model": "MODEL", "base_url": "BASE_URL", "temperature": "TEMPERATURE",
        "top_p": "TOP_P", "seed": "SEED", "max_tokens": "MAX_TOKENS",
        "max_retries": "MAX_RETRIES", "max_concurrency": "MAX_CONCURRENCY",
        "prompt_template": "PROMPT_TEMPLATE",
    }.items():
        require(config[field] == constants[constant], f"Config/script mismatch: {field}")

    data = json.loads(dataset_path.read_text())
    blind_path = BUNDLE / "test/eval_2wikimultihopqa_50.json"
    blind = {r["id"]: r for r in json.loads(blind_path.read_text())}
    gold_path = BUNDLE / "grading/gold_answers.jsonl"
    gold = {r["sample_id"]: r for r in read_rows(gold_path)}
    first = read_rows(run / "results.jsonl")
    retries = read_rows(run / "retry_results.jsonl")
    merged = read_rows(run / "merged_results.jsonl")
    first_by_id = {r["sample_id"]: r for r in first}
    retry_by_id = {r["sample_id"]: r for r in retries}
    merged_by_id = {r["sample_id"]: r for r in merged}
    expected_ids = {r["id"] for r in data}
    require(len(data) == len(first) == len(merged) == len(expected_ids) == 512, "Wrong sample count")
    require(expected_ids == set(first_by_id) == set(merged_by_id) == set(blind) == set(gold), "ID mismatch")
    require(len(retries) == len(retry_by_id) == 12, "Unexpected retry count")
    require(set(retry_by_id) == {r["sample_id"] for r in first if r["status"] == "ERROR"}, "Retry IDs differ")
    require(len(list((run / "trajectories").glob("*.json"))) == 512, "Wrong original trajectory count")
    journal = read_rows(run / "completed.jsonl")
    require(len(journal) == 512 and {r["sample_id"]: r for r in journal} == first_by_id, "Journal differs")

    for row in data:
        sid = row["id"]
        require(re.fullmatch(r"dev_\d+", sid) is not None, "Unsafe sample ID")
        require(all(row[k] == blind[sid][k] for k in ("input", "context", "type")), f"Bundle differs: {sid}")
        require(row["num_docs"] == blind[sid]["num_docs"] == 50, f"Document count differs: {sid}")
        require(row["answers"] == gold[sid]["gold_answers"], f"Gold differs: {sid}")
        first_row = first_by_id[sid]
        final = merged_by_id[sid]
        original_trace = json.loads((run / "trajectories" / f"{sid}.json").read_text())
        require(original_trace == {k: v for k, v in first_row.items() if k != "trajectory_path"}, f"Trace differs: {sid}")
        expected_final = first_row
        if sid in retry_by_id:
            require(retry_by_id[sid]["status"] == "OK", f"Retry failed: {sid}")
            expected_final = {**first_row, **retry_by_id[sid], "status": "OK", "recovered_on_retry": True}
        require(final == expected_final, f"Final merge differs: {sid}")
        require(final["status"] == "OK" and bool(final["raw_response"]), f"Missing response: {sid}")
        require(final["question"] == row["input"] and final["gold_answers"] == row["answers"], f"Question differs: {sid}")
        require(final["answer_exact"] == primary_exact(final["prediction"], row["answers"]), f"Score differs: {sid}")

    output.mkdir(parents=True)
    shutil.copytree(run, output / "raw_run")
    (output / "data").mkdir()
    shutil.copy2(dataset_path, output / "data/original_eval_512.json")
    shutil.copy2(blind_path, output / "data/submitted_blind_eval_512.json")
    shutil.copy2(gold_path, output / "data/gold_answers.jsonl")
    (output / "scripts").mkdir()
    for name in ("run_direct_full_context_512.py", "retry_direct_full_context_512_errors.py"):
        shutil.copy2(source / "scripts" / name, output / "scripts" / name)
    shutil.copy2(Path(__file__), output / "scripts/export_historical_direct512.py")

    index = []
    for row in data:
        sid = row["id"]
        final = merged_by_id[sid]
        prompt = config["prompt_template"].replace("{context}", row["context"]).replace("{question}", row["input"])
        payload = {
            "model": config["model"], "messages": [{"role": "user", "content": prompt}],
            "temperature": config["temperature"], "top_p": config["top_p"], "seed": config["seed"],
            "max_tokens": config["max_tokens"], "enable_thinking": config["enable_thinking"],
        }
        trace = {
            "schema_version": "historical-direct-export-v1",
            "sample_id": sid, "official_id": row["official_id"], "index": final["index"],
            "question": row["input"], "question_type": row["type"],
            "method": final["method"], "order": final["order"],
            "request": {
                "provenance": "reconstructed_from_hash_verified_original_script_config_and_dataset",
                "captured_during_run": False,
                "method": "POST", "url": config["base_url"] + "/chat/completions",
                "json_body": payload,
                "utf8_body_sha256": sha(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                "authorization_header": "omitted; no credential is included",
            },
            "recorded_stages": [
                {"stage": "first_pass", "source": "raw_run/results.jsonl", "result": first_by_id[sid]},
                *([{"stage": "retry_batch", "source": "raw_run/retry_results.jsonl", "result": retry_by_id[sid]}]
                  if sid in retry_by_id else []),
            ],
            "final_result": final,
            "response_text": final["raw_response"],
            "offline_evaluation": {
                "gold_answers": row["answers"], "prediction": final["prediction"],
                "answer_exact": final["answer_exact"],
                "metric": "punctuation-only normalized exact match",
                "gold_sent_in_request": False,
            },
            "recording_limits": {
                "raw_response_is": "choices[0].message.content, not the complete HTTP response JSON",
                "individual_http_attempts_recorded": False,
                "physical_attempt_count": None,
                "all_attempts_usage_available": False,
                "latency_includes": "queue wait (first pass), retries, and backoff; not pure inference time",
                "first_pass_error_field_may_remain_after_success": True,
            },
        }
        relative = f"trajectories/{sid}.json"
        write_json(output / relative, trace)
        index.append({
            "index": final["index"], "sample_id": sid, "official_id": row["official_id"],
            "question": row["input"], "question_type": row["type"],
            "status": final["status"], "prediction": final["prediction"],
            "gold_answers": row["answers"], "answer_exact": final["answer_exact"],
            "recovered_on_retry": bool(final.get("recovered_on_retry")),
            "first_pass_status": first_by_id[sid]["status"], "usage": final["usage"],
            "trajectory": relative,
        })
    index.sort(key=lambda r: r["index"])
    (output / "index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in index), encoding="utf-8")

    raw_hashes = {}
    for path in sorted(run.rglob("*")):
        if path.is_file():
            rel = path.relative_to(run).as_posix()
            raw_hashes[rel] = sha(path.read_bytes())
            require(sha((output / "raw_run" / rel).read_bytes()) == raw_hashes[rel], f"Copy differs: {rel}")
    audit = {
        "packaged_at": datetime.now(timezone.utc).isoformat(),
        "source_run": str(run), "new_model_calls": 0,
        "sample_count": 512, "exported_trajectory_count": 512,
        "first_pass_status": dict(Counter(r["status"] for r in first)),
        "retry_status": dict(Counter(r["status"] for r in retries)),
        "final_status": dict(Counter(r["status"] for r in merged)),
        "correct": sum(r["answer_exact"] for r in merged),
        "accuracy": sum(r["answer_exact"] for r in merged) / len(merged),
        "metric": "punctuation-only normalized exact match",
        "recorded_final_response_usage_only": {
            k: sum(r["usage"].get(k, 0) for r in merged)
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "physical_attempts_and_total_billed_usage": "unknown; individual attempts were not recorded",
        "original_dataset_sha256": config["data_sha256"],
        "submitted_blind_dataset_sha256": sha(blind_path.read_bytes()),
        "original_runner_sha256": config["script_sha256"],
        "checks": {
            "original_dataset_and_runner_match_recorded_hashes": True,
            "model_constants_and_prompt_match_config": True,
            "all_512_questions_contexts_types_match_submitted_bundle": True,
            "all_512_gold_answers_match_submitted_bundle": True,
            "all_512_primary_trajectories_match_primary_results": True,
            "completed_journal_matches_primary_results": True,
            "retry_ids_are_exactly_12_first_pass_failures": True,
            "merged_results_exactly_follow_recorded_retry_merge": True,
            "all_512_final_responses_present": True,
            "all_512_archived_primary_scores_recomputed": True,
            "raw_run_copied_byte_for_byte": True,
        },
        "first_pass_success_rows_with_stale_error_field": sum(r["status"] == "OK" and bool(r["error"]) for r in first),
        "raw_run_sha256": raw_hashes,
        "score_note": "Other historical metrics in raw_run/final_summary.json are preserved, not recomputed here.",
    }
    write_json(output / "audit.json", audit)
    readme = """# Direct 全上下文回答 512 题：轨迹导出包

本包整理的是 **2026-10-23** 已完成的历史运行 `direct_qwen35_9b_quality512_full_context_t0_r1`，
模型为 SiliconFlow `Qwen/Qwen3.5-9B`。本次导出没有重新调用模型。
它使用原始 50 文档全文提示，不是当前 MWR 共享答案协议下的新实验。

## 直接查看

- `index.jsonl`：按原题目顺序排列的 512 条索引，含题目、预测、标准答案、评分和相对轨迹路径。
- `trajectories/dev_*.json`：**512 份汇总轨迹**，含完整请求正文、模型原始输出文本、首轮记录、补跑记录（如有）、最终答案和 token 用量。
- `raw_run/`：原运行目录的逐字节副本。`results.jsonl` 是首轮，`retry_results.jsonl` 是 12 题补跑，`merged_results.jsonl` 是最终 512 题。
- `data/`：原始完整数据、用户提供的无答案推理数据、独立评分标注。两份输入的全部 512 个问题、上下文和类型均一致。
- `scripts/`：原运行脚本、补跑脚本及本次导出脚本。原运行脚本 SHA-256 与运行时配置保存的值一致。
- `audit.json`、`SHA256SUMS`：核验结果与逐文件校验和。

## 完整性与结果

首轮 512 题：500 条 OK、12 条 HTTP 429 限流错误。随后仅对这 12 题补跑，全部成功。
最终 512 条 OK，512 份最终模型输出文本均存在。按原记录主指标（转小写、去 ASCII 标点、合并空白后完全匹配）
复核为 **437 / 512 = 85.3515625%**。

模型参数：temperature=0、top_p=0.95、seed=4、enable_thinking=false、max_tokens=4096。
首轮并发为 8，每题最多尝试 6 次；后续补跑串行、每题最多尝试 6 次。
请求使用一条 user 消息，正文为全部 context 和问题，要求最终答案写在 `\\boxed{}` 中。
标准答案保存在离线结果中，没有作为标准答案字段传入模型请求。

最终成功响应记录的已知用量：输入 3,077,500 tokens、输出 90,961 tokens，共 3,168,461 tokens。
这不等于所有 HTTP 尝试的总账单；历史脚本没有逐次记录失败尝试及其用量。

## 记录边界（请保留这些说明）

1. **请求正文是还原值**：原脚本未存实际发送的请求报文；本包按哈希匹配的原脚本、配置和原始数据还原，
   逐题标记 `request.captured_during_run=false`。没有伪装成当时抓取的网络日志。
2. **模型输出是原始保存值**：`raw_response` / `response_text` 保留完整 `choices[0].message.content`，
   不是完整 HTTP JSON；响应 ID、响应头、finish_reason、每次失败尝试、未保存的中间响应均无法恢复。
   原运行关闭 thinking，本包不包含隐藏思维链。
3. 首轮脚本成功后未清除以前的 `error` 字段，222 条首轮 OK 记录仍含早前错误信息。
   原字段保持不变，应联合 `status`、`prediction` 和输出文本解读，不能仅凭非空 error 判为最终失败。
4. 原始 `raw_run/trajectories/` 保存首轮状态，12 条记录仍是 ERROR；它们的成功补跑见
   `retry_results.jsonl`。阅读最终完整记录请优先用本包顶层 `trajectories/`。
5. `latency_ms` 包含首轮排队时间、重试和退避，不能作为纯模型推理时延。
6. 原结果中的 `trajectory_path` 保留当时路径；便携索引 `index.jsonl` 的 `trajectory` 指向包内相对路径。
   `final_summary.json` 中其他历史评分口径原样保留，本次只复核上述主指标。
7. 本数据是经过模型无上下文错误筛选的困难子集；不能将其当作随机抽样的官方完整测试集。

包内没有 API 密钥。可在解压后的本目录执行 `shasum -a 256 -c SHA256SUMS` 核验文件。
"""
    (output / "README_中文.md").write_text(readme, encoding="utf-8")

    members = [p for p in sorted(output.rglob("*")) if p.is_file()]
    for path in members:
        require(SECRET_PATTERN.search(path.read_bytes()) is None, f"Credential-like literal detected: {path.name}")
    sums = "".join(f"{sha(p.read_bytes())}  {p.relative_to(output).as_posix()}\n" for p in members)
    (output / "SHA256SUMS").write_text(sums, encoding="utf-8")
    members.append(output / "SHA256SUMS")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(members):
            z.write(path, (Path(output.name) / path.relative_to(output)).as_posix())
    with zipfile.ZipFile(archive) as z:
        require(z.testzip() is None, "ZIP CRC verification failed")
        require(len(z.namelist()) == len(members), "ZIP member count mismatch")
        for path in members:
            name = (Path(output.name) / path.relative_to(output)).as_posix()
            require(sha(z.read(name)) == sha(path.read_bytes()), f"ZIP content mismatch: {name}")
    archive.with_suffix(".zip.sha256").write_text(f"{sha(archive.read_bytes())}  {archive.name}\n")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size,
                      "files": len(members), "trajectories": 512, "final_status": audit["final_status"],
                      "zip_crc_and_all_hashes_verified": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
