"""Audit a complete C run against the accepted B source-first run."""

import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.metrics import answer_exact, answer_token_f1


def read_json(path):
    return json.loads(path.read_text())


def read_rows(directory, frozen):
    expected = {sample["sample_id"]: sample for sample in frozen["samples"]}
    result = {}
    for line in (directory / "results.jsonl").read_text().splitlines():
        row = json.loads(line)
        key = (row["sample_id"], row["method"], row["order"])
        if key in result or key[1:] != ("v5_predicted", "original") or key[0] not in expected:
            raise ValueError(f"Duplicate or unexpected sample/condition: {key}")
        source = expected[key[0]]
        for field in ("question", "question_type", "gold_answers"):
            if row[field] != source[field]:
                raise ValueError(f"Frozen {field} mismatch for {key}")
        if row["answer_exact"] != answer_exact(row["prediction"], row["gold_answers"]):
            raise ValueError(f"Stored accuracy differs from frozen scorer: {key}")
        if abs(row["answer_token_f1"] - answer_token_f1(row["prediction"], row["gold_answers"])) > 1e-12:
            raise ValueError(f"Stored F1 differs from frozen scorer: {key}")
        result[key] = row
    if len(result) != 128 or {key[0] for key in result} != set(frozen["sample_ids"]):
        raise ValueError("Exactly 128 unique frozen samples are required, including errors")
    return result


def conditions(directory, frozen):
    config = read_json(directory / "config.resolved.json")
    manifest = read_json(directory / "code_manifest.json")
    for field in ("data_sha256", "tokenizer_sha256", "sample_ids"):
        if manifest[field] != frozen[field]:
            raise ValueError(f"Run manifest does not match frozen {field}")
    if config.get("reader_api") is not None:
        raise ValueError("Expected one shared high/low model client")
    for key in ("experiment_id", "output_dir", "config_fingerprint"):
        config.pop(key, None)
    return config


def trajectory(directory, row):
    return read_json(directory / "trajectories" / Path(row["trajectory_path"]).name)


def audit_c_trace(trace):
    counts = Counter()
    violations = []
    prechecked = False
    for event in trace["events"]:
        kind = event["event_type"]
        payload = event["payload"]
        counts[kind] += 1
        if kind == "MEMORY_EVIDENCE_PREPARED":
            prechecked = False
        elif kind == "MEMORY_VERIFY_COMPLETED":
            if payload.get("mode") != "source_precheck":
                violations.append("non-precheck MEMORY_VERIFY event")
            else:
                prechecked = True
                counts.update("verdict_" + check["verdict"] for check in payload["checks"])
        elif kind == "AGENT_CALLED" and payload.get("interface") == "MEMORY":
            if not prechecked:
                violations.append("MEMORY called without current source precheck")
        if kind.startswith("RAW_FALLBACK") or any(
            marker in kind for marker in ("EVIDENCE_RESCUE", "SOFT_RESCUE", "SOURCE_ADJUDICATION")
        ):
            violations.append("forbidden event: " + kind)
    for call in trace["model_calls"]:
        interface = call["interface"]
        counts["calls_" + interface] += 1
        expected_role = "HIGH" if interface in {"PLAN", "MEMORY", "MEMORY_VERIFY"} else "LOW"
        if call.get("agent_role") != expected_role:
            violations.append("incorrect role: " + interface)
        if interface == "VERIFY":
            violations.append("low-level VERIFY called")
        if interface == "MEMORY_VERIFY":
            messages = (call.get("raw_request") or {}).get("messages", [])
            if not any("mode=SOURCE_PRECHECK" in message.get("content", "") for message in messages):
                violations.append("MEMORY_VERIFY used a non-precheck prompt")
    return counts, sorted(set(violations))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    frozen = read_json(ROOT / "reports/source_first/frozen_manifest.json")
    previous = read_rows(args.baseline, frozen)
    current = read_rows(args.candidate, frozen)
    before_config = conditions(args.baseline, frozen)
    after_config = conditions(args.candidate, frozen)
    if before_config["runner"]["memory_source_mode"] != "raw_before_memory":
        raise ValueError("Baseline is not B raw-before-memory")
    if after_config["runner"]["memory_source_mode"] != "source_verify_before_memory":
        raise ValueError("Candidate is not C source-verify-before-memory")
    before_config["runner"]["memory_source_mode"] = "source_verify_before_memory"
    if before_config != after_config:
        raise ValueError("Effective B/C settings differ beyond memory_source_mode and run identity")
    manifest = read_json(args.candidate / "code_manifest.json")
    for name, digest in manifest["source_sha256"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Production code changed during candidate run: " + name)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    paired = []
    losses = []
    gains = []
    audit_counts = Counter()
    violations = {}
    for sample_id in frozen["sample_ids"]:
        key = (sample_id, "v5_predicted", "original")
        before, after = previous[key], current[key]
        trace = trajectory(args.candidate, after)
        counts, problems = audit_c_trace(trace)
        audit_counts.update(counts)
        if problems:
            violations[sample_id] = problems
        flip = "STABLE"
        if bool(before["answer_exact"]) != bool(after["answer_exact"]):
            flip = "GAIN" if after["answer_exact"] else "LOSS"
            (gains if flip == "GAIN" else losses).append(sample_id)
        paired.append({
            "sample_id": sample_id, "question_type": before["question_type"], "flip": flip,
            "b_prediction": before["prediction"], "c_prediction": after["prediction"],
            "b_exact": before["answer_exact"], "c_exact": after["answer_exact"],
            "b_f1": before["answer_token_f1"], "c_f1": after["answer_token_f1"],
            "b_status": before["status"], "c_status": after["status"],
            "b_model_calls": before["model_calls"], "c_model_calls": after["model_calls"],
            "b_input_tokens": before["input_tokens"], "c_input_tokens": after["input_tokens"],
            "b_output_tokens": before["output_tokens"], "c_output_tokens": after["output_tokens"],
            "b_latency_ms": before["latency_ms"], "c_latency_ms": after["latency_ms"],
        })
    with (args.report_dir / "paired_diff.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(paired[0]))
        writer.writeheader()
        writer.writerows(paired)

    def metrics(rows):
        return {
            "exact": sum(bool(row["answer_exact"]) for row in rows.values()),
            "f1": sum(row["answer_token_f1"] for row in rows.values()) / 128,
            "errors": sum(row["status"] == "ERROR" for row in rows.values()),
            "resource_limits": sum(row["runtime_status"] == "RESOURCE_LIMIT" for row in rows.values()),
            "protocol_failures": sum(row.get("protocol_valid") is False for row in rows.values()),
            "model_calls": sum(row["model_calls"] for row in rows.values()),
            "input_tokens": sum(row.get("input_tokens") or 0 for row in rows.values()),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in rows.values()),
            "latency_ms": sum(row.get("latency_ms") or 0 for row in rows.values()),
            "by_type": {kind: {
                "correct": sum(row["answer_exact"] for row in rows.values() if row["question_type"] == kind),
                "count": sum(row["question_type"] == kind for row in rows.values()),
            } for kind in frozen["question_types"]},
        }

    b, c = metrics(previous), metrics(current)
    if c["exact"] - b["exact"] != len(gains) - len(losses):
        raise ValueError("Paired gains and losses do not reconcile with exact count")
    status = "ACCEPTED" if c["exact"] >= b["exact"] and not violations else "BLOCKED"
    gate = {
        "stage": "C", "compared_to": "B", "status": status,
        "baseline_run": str(args.baseline), "candidate_run": str(args.candidate),
        "sample_count": 128, "baseline": b, "candidate": c,
        "gains": gains, "losses": losses, "audit_counts": dict(audit_counts),
        "violations": violations, "data_sha256": manifest["data_sha256"],
        "tokenizer_sha256": manifest["tokenizer_sha256"],
    }
    (args.report_dir / "c_gate.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n")
    lines = ["# C 对 B 的退步题", "", "以下是逐题结果定位；原因需结合对应原始轨迹确认。", ""]
    for sample_id in losses:
        key = (sample_id, "v5_predicted", "original")
        before, after = previous[key], current[key]
        lines.extend([
            f"## {sample_id}", "", before["question"], "",
            f"Gold: {json.dumps(before['gold_answers'], ensure_ascii=False)}", "",
            f"B: {before['prediction']} ({before['status']})", "",
            f"C: {after['prediction']} ({after['status']})", "",
            f"B trajectory: {before['trajectory_path']}", "",
            f"C trajectory: {after['trajectory_path']}", "",
        ])
    (args.report_dir / "c_regressions.md").write_text("\n".join(lines))
    print(json.dumps({"status": status, "b_exact": b["exact"], "c_exact": c["exact"],
                      "gains": len(gains), "losses": len(losses), "violations": len(violations)}, indent=2))
    return 0 if status == "ACCEPTED" else 2


if __name__ == "__main__":
    sys.exit(main())
