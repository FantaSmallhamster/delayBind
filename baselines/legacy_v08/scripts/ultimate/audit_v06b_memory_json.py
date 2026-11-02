"""Audit the V06b JSON MEMORY run against V06a and the retained parent."""

import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from delaybind_core.memory_json import grounded_memory_response_format, memory_response_format
from scripts.ultimate.audit_three_column_update import audit_run, read_json
from delaybind_core.update_json import update_response_format


def normalized_config(directory):
    value = read_json(directory / "config.resolved.json")
    for key in ("experiment_id", "output_dir", "config_fingerprint"):
        value.pop(key, None)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--v06a", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    candidate, metrics, manifest = audit_run(args.candidate, candidate=True, json_update=True)
    v06a, v06a_metrics, v06a_manifest = audit_run(args.v06a, candidate=False)
    parent, parent_metrics, parent_manifest = audit_run(args.parent, candidate=False)
    for other in (v06a_manifest, parent_manifest):
        for key in ("sample_ids", "data_sha256", "tokenizer_sha256"):
            assert manifest[key] == other[key], key
    cfg = read_json(args.candidate / "config.resolved.json")["runner"]
    assert cfg["memory_protocol"] == "json" and cfg["memory_json_mode"] in {"json_schema", "json_object", "prompt"}
    format_violations = []
    event_counts = {"MEMORY_JSON_REJECTED": 0, "MEMORY_GROUNDED_JSON_REJECTED": 0,
                    "MEMORY_SOURCE_CHECK_REJECTED": 0, "MEMORY_LINE_REJECTED": 0}
    paired = []
    for sid in manifest["sample_ids"]:
        trace = read_json(args.candidate / "trajectories" / Path(candidate[sid]["trajectory_path"]).name)
        for call in trace["model_calls"]:
            expected = None
            if call["interface"] == "UPDATE":
                expected = update_response_format(cfg["update_json_mode"])
            elif call["interface"] == "MEMORY":
                expected = memory_response_format(cfg["memory_json_mode"])
            elif call["interface"] == "MEMORY_GROUNDED":
                expected = grounded_memory_response_format(cfg["memory_json_mode"])
            if call["interface"] in {"UPDATE", "MEMORY", "MEMORY_GROUNDED"} and call["raw_request"].get("response_format") != expected:
                format_violations.append({"sample_id": sid, "interface": call["interface"]})
        for event in trace["events"]:
            event_counts[event["event_type"]] = event_counts.get(event["event_type"], 0) + 1
        for label, old in (("v06a", v06a), ("parent", parent)):
            flip = "STABLE" if old[sid]["answer_exact"] == candidate[sid]["answer_exact"] else "GAIN" if candidate[sid]["answer_exact"] else "LOSS"
            paired.append({"baseline": label, "sample_id": sid, "question_type": candidate[sid]["question_type"],
                           "flip": flip, "question": candidate[sid]["question"],
                           "baseline_prediction": old[sid]["prediction"], "candidate_prediction": candidate[sid]["prediction"],
                           "baseline_exact": old[sid]["answer_exact"], "candidate_exact": candidate[sid]["answer_exact"]})
    assert not format_violations and not metrics["violations"]
    comparisons = {}
    for label, old_metrics in (("v06a", v06a_metrics), ("parent", parent_metrics)):
        rows = [row for row in paired if row["baseline"] == label]
        comparisons[label] = {"correct_delta": metrics["correct"] - old_metrics["correct"],
                              "accuracy_delta": metrics["accuracy"] - old_metrics["accuracy"],
                              "f1_delta": metrics["f1"] - old_metrics["f1"],
                              "gains": [row["sample_id"] for row in rows if row["flip"] == "GAIN"],
                              "losses": [row["sample_id"] for row in rows if row["flip"] == "LOSS"]}
    result = {"stage": "V06b", "status": "ONE_RUN_COMPLETE", "candidate_run": str(args.candidate),
              "candidate": metrics, "comparisons": comparisons, "format_violations": format_violations,
              "event_counts": event_counts,
              "strict_stage_acceptance": "Not established: this is one fresh run; the predeclared three-run paired gate remains outstanding.",
              "invalid_prior_run": "results/ultimate/v06b_memory_json_full128_r1 (retry1 used text MEMORY because config mapping was incomplete).",
              "intermediate_prior_run": "results/ultimate/v06b_memory_json_full128_r1_retry2 (fact-text checks were initially rejected; not used for acceptance)."}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (args.report_dir / "paired_diff.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(paired[0]))
        writer.writeheader(); writer.writerows(paired)
    print(json.dumps({"candidate": {k: metrics[k] for k in ("correct", "accuracy", "f1", "statuses", "runtime_statuses", "replayed_states")},
                      "comparisons": {k: {n: v[n] for n in ("correct_delta", "f1_delta", "gains", "losses")} for k, v in comparisons.items()},
                      "format_violations": format_violations, "event_counts": event_counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
