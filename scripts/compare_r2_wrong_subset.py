"""Compare a fresh R2 run with the merged 128-question baseline's wrong cases."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def rows(path: Path) -> dict[str, dict]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        sample_id = row["sample_id"]
        if sample_id in result:
            raise ValueError(f"duplicate sample ID in {path}: {sample_id}")
        result[sample_id] = row
    return result


def cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        value = "; ".join(map(str, value))
    return str(value).replace("|", "\\|").replace("\n", " ").strip() or "—"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--retry", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    baseline = rows(args.baseline)
    retry = rows(args.retry)
    if len(baseline) != 128 or not set(retry) <= set(baseline):
        raise ValueError("baseline must contain 128 questions and all retry IDs")
    merged = {**baseline, **retry}
    wrong = {sid: row for sid, row in merged.items() if not row.get("answer_exact")}
    old_correct = len(merged) - len(wrong)
    current = rows(args.current)
    if set(current) != set(wrong):
        raise ValueError(
            f"current IDs must equal baseline wrong IDs: missing={sorted(set(wrong)-set(current))}, "
            f"extra={sorted(set(current)-set(wrong))}"
        )
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if set(config.get("sample_ids", [])) != set(wrong):
        raise ValueError("run config sample IDs differ from merged baseline wrong IDs")

    fixed = sorted((sid for sid, row in current.items() if row.get("answer_exact")), key=lambda s: int(s.split("_")[1]))
    old_statuses = Counter(row.get("runtime_status") or row.get("status") or "UNKNOWN" for row in wrong.values())
    statuses = Counter(row.get("runtime_status") or row.get("status") or "UNKNOWN" for row in current.values())
    ordered = sorted(wrong, key=lambda s: int(s.split("_")[1]))
    lines = [
        "# R2 当前版本：历史 128 题错题复测",
        "",
        f"- 合并基线：{old_correct}/128 正确；{len(wrong)} 道错题。",
        f"- 本次仅重测这 {len(wrong)} 道：{len(fixed)} 道转为 exact match 正确，{len(wrong)-len(fixed)} 道仍未匹配。",
        f"- 错题子集的新 exact match：{len(fixed)}/{len(wrong)}（{len(fixed)/len(wrong):.1%}）。",
        f"- 模型：{cell(config['api']['model'])}；温度：{config['api']['temperature']}；数据：{cell(config['input'])}。",
        f"- 原先正确的 {old_correct} 道未重跑；本报告不把子集结果当作新一轮 128 题总准确率。",
        "",
        "## 运行状态",
        "",
        "| 状态 | 旧错题记录 | 当前复测 |",
        "| --- | ---: | ---: |",
        *(f"| {cell(status)} | {old_statuses[status]} | {statuses[status]} |"
          for status in sorted(set(old_statuses) | set(statuses))),
        "",
        "## 转对的题",
        "",
        ", ".join(fixed) if fixed else "无。",
        "",
        "## 逐题对照",
        "",
        "| 题号 | 旧答案 | 新答案 | 金标准 | 新状态 | 结果 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for sid in ordered:
        old, new = wrong[sid], current[sid]
        lines.append(
            f"| {cell(sid)} | {cell(old.get('prediction'))} | {cell(new.get('prediction'))} "
            f"| {cell(new.get('gold_answers'))} | {cell(new.get('runtime_status') or new.get('status'))} "
            f"| {'转对' if new.get('answer_exact') else '仍错'} |"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"baseline_wrong": len(wrong), "retested": len(current), "fixed": len(fixed),
                      "still_wrong": len(wrong)-len(fixed), "new_statuses": dict(statuses),
                      "report": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
