"""Freeze a blind MuSiQue dev subset using ReMemR1's document expansion recipe.

No model calls. Upstream sources are pinned and inspected with AST; their module
imports (including model downloads and server setup) are never executed.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import shutil
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.tokenization import TokenizerJSON

COMMIT = "cc514c092ca968a50c52cdcc2e2ba96362fce25a"
UPSTREAM = ROOT / "reports/mwr/musique_freeze_20261029/upstream"
SOURCE = ROOT / "data/raw/musique_v1.0/musique_ans_v1.0_dev.jsonl"
TOKENIZER = ROOT / "models/Qwen3.5-9B-tokenizer/tokenizer.json"
DEFAULT_DEST = ROOT / "data/test/musique_dev512_rememr1_50_seed42_20261029"


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def literal(path: Path, name: str):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError(f"Missing literal {name} in {path}")


def selected_function(path: Path, name: str, namespace: dict):
    """Execute only an inspected top-level function, never upstream module code."""
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(nodes) != 1:
        raise ValueError(f"Missing or ambiguous function {name}")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def doc_text(paragraph: dict) -> str:
    return paragraph["title"] + "\n" + paragraph["paragraph_text"]


def question_text(row: dict) -> str:
    # Exactly the normalization in upstream read_dataset.
    question = row["question"].strip()
    return question if question.endswith("?") else question + "?"


def expand_indices(original: list[int], pool_size: int, count: int, rng: random.Random):
    if not len(original) <= count < pool_size:
        raise ValueError("Require original document count <= target < pool size")
    excluded = set(original)
    candidates = [i for i in range(pool_size) if i not in excluded]
    added = rng.sample(candidates, count - len(original))
    before_shuffle = original + added
    perm = list(range(count))
    random.Random(4).shuffle(perm)
    return [before_shuffle[i] for i in perm], added, perm


def make_payload(row: dict, config: dict, template: str, tokenizer):
    prompt = template.replace("$DOC$", row["context"].strip()).replace("$Q$", row["input"].strip())
    ids = tokenizer.encode(prompt)
    original_length = len(ids)
    maximum = config["max_input_tokens"]
    if original_length > maximum:
        ids = ids[:maximum // 2] + ids[-maximum // 2:]
        # Upstream explicitly uses skip_special_tokens=True after truncation.
        prompt = tokenizer.tokenizer.decode(ids, skip_special_tokens=True)
    payload = {"model": config["model"], "messages": [{"role": "user", "content": prompt}],
               "temperature": config["temperature"], "top_p": config["top_p"],
               "max_tokens": config["max_output_tokens"]}
    return payload, {"prompt_tokens_before_truncation": original_length,
                     "prompt_tokens_after_truncation": len(tokenizer.encode(prompt)),
                     "truncated": original_length > maximum}


def stats(values):
    values = sorted(values)
    return {"min": values[0], "mean": round(statistics.mean(values), 2),
            "median": statistics.median(values), "p95": values[math.ceil(len(values) * .95) - 1],
            "max": values[-1]}


def freeze(dest: Path, count: int = 512, documents: int = 50, seed: int = 42):
    if dest.exists():
        raise ValueError("Refusing to overwrite a frozen bundle; choose a new destination")
    rows = [json.loads(line) for line in SOURCE.read_text().splitlines()]
    if not 0 < count <= len(rows):
        raise ValueError("Invalid sample count")
    assert len({r["id"] for r in rows}) == len(rows)
    upstream_manifest = json.loads((UPSTREAM / "manifest.json").read_text())
    assert upstream_manifest["commit"] == COMMIT
    for entry in upstream_manifest["files"]:
        assert sha((UPSTREAM / entry["path"]).read_bytes()) == entry["sha256"]
    template = literal(UPSTREAM / "taskutils/memory_eval/utils/openai_api.py", "template_0shot")
    rng = random.Random(seed)
    candidate_indices = rng.sample(range(len(rows)), min(5 * count, len(rows)))
    selection = candidate_indices[:count]
    pool = sorted({doc_text(p) for row in rows for p in row["paragraphs"]})
    lookup = {text: i for i, text in enumerate(pool)}
    occurrences = defaultdict(list)
    for row in rows:
        for p in row["paragraphs"]:
            occurrences[lookup[doc_text(p)]].append([row["id"], p["idx"]])
    tok = TokenizerJSON(TOKENIZER)
    config = {
        "profile": "rememr1-direct-openai-locked-v1", "upstream_commit": COMMIT,
        "model": "Qwen/Qwen3.5-9B", "base_url": "https://api.siliconflow.cn/v1",
        "temperature": 0.7, "top_p": 0.95, "max_input_tokens": 120000,
        "max_output_tokens": 10000, "repeats": 1, "system_message": None,
        "seed": None, "enable_thinking": None, "response_format": None,
        "omitted_request_fields": ["seed", "enable_thinking", "response_format", "stop"],
        "thinking_behavior": "Not specified by upstream request; provider/model default applies. Not forced false.",
        "truncation": "tokenize full prompt; if >120000 keep first60000+last60000; decode skip_special_tokens=True",
        "answer_parser": "upstream extract_solution then extract_answer; FORCE_THINK unset",
        "max_attempts_per_question": 1, "semantic_retries": 0,
        "cache": False, "concurrency": 1, "transport_timeout_seconds": 1800,
        "transport_note": "Serial local adapter; timeout/concurrency differ from upstream high-throughput runner.",
        "model_note": "User-selected Qwen3.5-9B on SiliconFlow, not the paper checkpoint or serving engine.",
        "tokenizer_path": str(TOKENIZER.relative_to(ROOT)),
        "tokenizer_sha256": sha(TOKENIZER.read_bytes()),
        "prompt_template_sha256": sha(template.encode()), "model_calls_at_freeze": 0,
    }
    blind, gold, compat, maps, requests, per_sample = [], [], [], [], [], []
    source_function = UPSTREAM / "taskutils/data_synthesis/process_test.py"
    for position, index in enumerate(selection):
        row = rows[index]
        original = [lookup[doc_text(p)] for p in row["paragraphs"]]
        by_idx = {p["idx"]: i for i, p in enumerate(row["paragraphs"])}
        evidence_idx = [by_idx[s["paragraph_support_idx"]] for s in row["question_decomposition"]]
        assert set(evidence_idx) == {i for i, p in enumerate(row["paragraphs"]) if p["is_supporting"]}
        assert row["answerable"] is True
        qa = {"question": question_text(row), "outputs": [row["answer"], *row["answer_aliases"]],
              "context": original, "evidence_idx": evidence_idx,
              "level": None, "type": row["id"].split("__")[0], "id": row["id"]}
        # Differential check against the exact upstream expansion function.
        reference_rng = random.Random()
        reference_rng.setstate(rng.getstate())
        expanded, added, perm = expand_indices(original, len(pool), documents, rng)
        random_proxy = type("RandomProxy", (), {"sample": staticmethod(reference_rng.sample), "Random": random.Random})
        upstream_generate = selected_function(source_function, "generate_input_output",
                                              {"QAS": [qa], "DOCS": pool, "random": random_proxy})
        upstream = upstream_generate(0, documents)
        pieces = [f"Document {i + 1}:\n{pool[pid]}" for i, pid in enumerate(expanded)]
        context = "\n\n".join(pieces)
        support_positions = [perm.index(i) for i in evidence_idx]
        assert context == upstream["context"]
        assert support_positions == upstream["evidence_idx"]
        assert rng.getstate() == reference_rng.getstate()
        assert all(expanded[perm.index(i)] == pid for i, pid in enumerate(original))
        assert not set(added) & set(original) and len(set(added)) == len(added)
        inference = {"id": row["id"], "type": qa["type"], "input": qa["question"],
                     "context": context, "num_docs": documents}
        blind.append(inference)
        upstream["index"] = index
        compat.append(upstream)
        docmap, offset = [], 0
        for display, (pool_index, old_position, piece) in enumerate(zip(expanded, perm, pieces)):
            original_p = row["paragraphs"][old_position] if old_position < len(original) else None
            start = offset + len(f"Document {display + 1}:\n")
            end = start + len(pool[pool_index])
            assert context[start:end] == pool[pool_index]
            docmap.append({"display_document_number": display + 1, "display_index": display,
                           "pool_index": pool_index, "text_sha256": sha(pool[pool_index].encode()),
                           "official_paragraph_idx": original_p["idx"] if original_p else None,
                           "original_position": old_position if original_p else None,
                           "origin": "original" if original_p else "sampled_same_dev_pool",
                           "context_content_start": start, "context_content_end": end})
            offset += len(piece) + 2
        maps.append({"sample_id": row["id"], "added_pool_indices_before_shuffle": added,
                     "permutation": perm, "documents": docmap})
        gold.append({"sample_id": row["id"], "official_id": row["id"],
                     "question": row["question"], "answer": row["answer"],
                     "gold_answers": list(dict.fromkeys(qa["outputs"])),
                     "answer_aliases": row["answer_aliases"], "answerable": True,
                     "supporting_paragraph_ids": [p["idx"] for p in row["paragraphs"] if p["is_supporting"]],
                     "evidence_idx": support_positions, "question_decomposition": row["question_decomposition"],
                     "support_annotation_level": "paragraph; expanded positions are zero-based",
                     "added_document_annotation": "Unlabelled for this question; no guarantee of semantic irrelevance."})
        payload, lengths = make_payload(inference, config, template, tok)
        assert not lengths["truncated"]
        requests.append({"sample_id": row["id"], "request_sha256": sha(canonical(payload)), "json_body": payload})
        per_sample.append({"sample_id": row["id"], "source_index": index,
                           "original_document_count": len(original), "added_document_count": len(added),
                           "context_tokens": len(tok.encode(context)), **lengths})
    dest.mkdir(parents=True)
    shutil.copytree(UPSTREAM, dest / "provenance/upstream")
    metric_dir = dest / "provenance/musique"
    metric_dir.mkdir(parents=True)
    shutil.copy2(ROOT / "data/raw/musique_v1.0/provenance/metrics/answer.py", metric_dir / "answer.py")
    write_json(dest / "inference/eval_musique_50.json", blind)
    write_json(dest / "upstream_compatible/eval_musique_50.json", compat)
    write_jsonl(dest / "grading/gold_answers.jsonl", gold)
    write_jsonl(dest / "source/selected_official.jsonl", [rows[i] for i in selection])
    write_jsonl(dest / "metadata/document_map.jsonl", maps)
    write_jsonl(dest / "metadata/doc_pool_index.jsonl", (
        {"pool_index": i, "title": text.split("\n", 1)[0], "text_sha256": sha(text.encode()),
         "source_occurrences": occurrences[i]} for i, text in enumerate(pool)))
    write_json(dest / "metadata/selected_ids.json", [r["id"] for r in blind])
    write_json(dest / "metadata/token_lengths.json", per_sample)
    write_json(dest / "config/direct.json", config)
    (dest / "config/prompt_template.txt").write_text(template, encoding="utf-8")
    write_jsonl(dest / "requests/direct_requests.jsonl", requests)
    report = {"sample_count": count, "documents_per_question": documents,
              "pool_unique_documents": len(pool), "type_counts": dict(Counter(r["type"] for r in blind)),
              "context_tokens": stats([r["context_tokens"] for r in per_sample]),
              "prompt_tokens": stats([r["prompt_tokens_before_truncation"] for r in per_sample]),
              "truncated_requests": 0, "model_calls": 0,
              "upstream_expansion_differential_checks_passed": count,
              "all_original_paragraphs_preserved": True,
              "all_original_support_mappings_verified": True,
              "inference_fields": sorted(blind[0]), "answer_labels_in_inference": False}
    write_json(dest / "metadata/build_audit.json", report)
    write_json(dest / "metadata/selection.json", {
        "seed": seed, "population": len(rows), "requested_count": count,
        "candidate_indices": candidate_indices, "selected_source_indices": selection,
        "algorithm": "Random(seed).sample(range(N), min(5*n,N))[:n]",
        "rng_schedule": "One Random(seed): candidate selection, then selected questions serially in selection order.",
        "selection_uses_model_outputs": False, "selection_uses_gold_difficulty": False,
        "question_selection_independent_of_answers": True,
        "permutation_seed": 4, "expansion_pool": "all paragraphs of MuSiQue-Ans dev only",
        "pool_algorithm": "sorted(set(title + newline + paragraph_text))",
        "extra_filtering": "None beyond excluding original document text IDs; no answer/entity/relevance filters.",
        "deviations_from_upstream": [
            "Native MuSiQue schema adapted instead of FlashRAG; paragraph support indices preserved.",
            "Cap 5*n candidates at N: upstream requests 2560 from only 2417 and would fail.",
            "Serial explicit RNG state replaces multiprocessing-dependent random state.",
            "Frozen selection shared across future conditions, without model-based question filtering.",
            "Gold is stored separately; an upstream-compatible evaluator file is also provided.",
        ]})
    (dest / "README.md").write_text(readme(dest, report), encoding="utf-8")
    provenance_scripts = [Path(__file__), ROOT / "scripts/mwr/run_frozen_musique_direct.py"]
    for script in provenance_scripts:
        target = dest / "provenance/local_scripts" / script.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(script, target)
    files = {str(p.relative_to(dest)): {"bytes": p.stat().st_size, "sha256": sha(p.read_bytes())}
             for p in sorted(dest.rglob("*")) if p.is_file()}
    manifest = {"schema_version": "musique-rememr1-frozen-v1", "frozen": True,
                "source_split": "MuSiQue-Ans v1.0 dev; selected for development-based testing, not official test",
                "source_sha256": sha(SOURCE.read_bytes()), "source_path": str(SOURCE.relative_to(ROOT)),
                "source_pool_scope": "entire dev split; questions outside selected subset may supply text only",
                "upstream_commit": COMMIT, "python_version": platform.python_version(),
                "model_calls_at_freeze": 0, "summary": report, "files": files}
    write_json(dest / "metadata/frozen_manifest.json", manifest)
    checksum_files = [p for p in sorted(dest.rglob("*")) if p.is_file()]
    (dest / "SHA256SUMS").write_text("".join(f"{sha(p.read_bytes())}  {p.relative_to(dest)}\n" for p in checksum_files))
    return report


def readme(dest, report):
    rel = str(dest.relative_to(ROOT)) if dest.is_relative_to(ROOT) else str(dest)
    return f'''# MuSiQue dev 冻结子集：ReMemR1 扩展与 Direct 协议

本包包含 {report['sample_count']} 题，每题 {report['documents_per_question']} 篇文档，来源为 MuSiQue-Ans v1.0 dev。所有文件已冻结哈希，模型调用为 0。它是开发集派生测试子集，不是官方独立 test，也没有按模型答错情况筛选。

## 扩展方式

对齐 ReMemR1 固定提交 `{COMMIT}` 的 `taskutils/data_synthesis/process_test.py`：同一 dev 的所有标题+正文组成排序去重池（{report['pool_unique_documents']} 篇），保留题目全部原始段落，从排除原始文本后的池中无放回随机补足 50 篇，以 `Random(4).shuffle` 打乱。文档格式为 `Document N:\\n标题\\n正文`，段落间两个换行，无结尾附加换行。

选择种子为 42；先取 `sample(range(2417), min(5*512,2417))` 的前 512 题，再用同一 RNG 串行抽样干扰文档。原程序会因 2560>2417 报错，且多进程 RNG 调度不固定；这两项适配明确记录在 `metadata/selection.json`。逐题运行隔离的上游原始扩展函数，{report['sample_count']} 题的 context 与支持位置全部相同。

不按答案、实体或相似度额外排除文档，也不造假文档。抽入的段落可能偶然提供有效证据，不能将所有新增段落宣称为经人工验证的无关文档。证据位置、原始 idx、字符区间和每篇文档来源均可追溯。原支持标注保留为段落级，新增文本没有补造支持标注。

## Direct 设置

对齐上游 `run_eval.py` 中标准 Direct 的 `method="openai"`，不是 optional boxed 路径，也不是本项目 `direct_mwr` 的 JSON 提示。

- temperature=0.7，top_p=0.95，max_tokens=10000，重复次数 1。
- 单条 user 消息，无 system 消息；完整 prompt 原文在 `config/prompt_template.txt`。
- 上游未发送 seed、enable_thinking、response_format、stop，本配置也不发送。thinking 使用提供方/模型默认值，**没有沿用先前的 thinking=false**。
- 输入上限 120000 tokens；超过时保留前后各 60000。当前所有冻结请求都未截断。
- 沿用用户指定 SiliconFlow `Qwen/Qwen3.5-9B`，不是论文模型权重/推理后端。因此是扩展算法、prompt 和采样请求参数对齐，不声称完整复现论文分数；服务器默认设置不由本文件控制。
- 单次请求、不重试错误答案、不缓存。串行调用和 1800 秒传输超时是本机适配；与上游并发/超时不同。
- 上游答案解析：丢弃最后一个 `</think>` 之前内容，再提取最后一个小写 `the answer is` 后文本。格式失败也计入总分母。

平均上下文 {report['context_tokens']['mean']} tokens，范围 {report['context_tokens']['min']}–{report['context_tokens']['max']}；按当前 Qwen3.5 tokenizer 实测，不含聊天模板封装。

## 文件与使用

- `inference/eval_musique_50.json`：盲推理输入，只有 id/type/input/context/num_docs。
- `grading/gold_answers.jsonl`：答案、别名、原始支持标注及扩展后的零基位置。
- `requests/direct_requests.jsonl`：全部冻结请求正文，无密钥；尚未发送。
- `upstream_compatible/eval_musique_50.json`：用于上游 test_qa.py 的完整评测格式，含 answers 和 evidence_idx；**不能把整个记录序列化为模型提示**。
- `metadata/`：选择规则、完整 ID、文档映射、token 长度、哈希及审计。
- `source/selected_official.jsonl`：所选原始记录，用于离线评测和溯源。
- `provenance/upstream/`：在线下载的固定提交源码，已与本地版本核对。

在项目根目录执行无模型检查：

```bash
.venv/bin/python scripts/mwr/run_frozen_musique_direct.py --bundle {rel}
```

需要实际运行时，在本机环境设置 MODEL_API_KEY，再执行以下命令；模型/URL 读取冻结配置，不读取可变 MODEL_NAME/MODEL_BASE_URL：

```bash
.venv/bin/python scripts/mwr/run_frozen_musique_direct.py --bundle {rel} --execute --output results/musique_dev512_rememr1_direct_r1
```

输出包含每题完整请求/HTTP JSON 响应、usage、解析结果和状态；评测在推理之后独立读取 gold。上游主答案 EM/F1 与 MuSiQue 别名 EM/F1 分开报告，失败或缺失始终计入 512 分母（上游会跳过空响应，本适配保留失败分母）。不将 Direct 无支持预测的情况伪报成支持召回成绩。随机采样响应不保证重复运行相同。

检查冻结文件：`cd {rel} && shasum -a 256 -c SHA256SUMS`。
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--count", type=int, default=512)
    parser.add_argument("--documents", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    print(json.dumps(freeze(args.output.resolve(), args.count, args.documents, args.seed), indent=2))
