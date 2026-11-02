"""Audit the V08 final evidence pack and source-assessment run."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.ultimate.audit_three_column_update import audit_run, read_json


def normalized_config(directory):
    value = read_json(directory / "config.resolved.json")
    for key in ("experiment_id", "output_dir", "config_fingerprint"):
        value.pop(key, None)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--parent", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()

    candidate, metrics, manifest = audit_run(args.candidate, candidate=True, json_update=True)
    parent, parent_metrics, parent_manifest = audit_run(args.parent, candidate=False)
    for key in ("sample_ids", "data_sha256", "tokenizer_sha256"):
        assert manifest[key] == parent_manifest[key], key
    assert normalized_config(args.candidate) == normalized_config(args.parent)

    violations = []
    counters = Counter()
    paired = []
    for sample_id in manifest["sample_ids"]:
        row = candidate[sample_id]
        trace = read_json(args.candidate / "trajectories" / Path(row["trajectory_path"]).name)
        for event in trace["events"]:
            counters[event["event_type"]] += 1
            if event["event_type"] == "FINAL_ANSWER_REQUEST_MEASURED":
                payload = event["payload"]
                if payload["limit"] is not None and payload["size"] > payload["limit"]:
                    violations.append({"sample_id": sample_id, "reason": "answer request over budget"})
        if row["status"] == "OK":
            if not any(event["event_type"] == "FINAL_EVIDENCE_PACK_BUILT" for event in trace["events"]):
                violations.append({"sample_id": sample_id, "reason": "missing final pack event"})
            if not any(event["event_type"] == "FINAL_ANSWER_REQUEST_MEASURED" for event in trace["events"]):
                violations.append({"sample_id": sample_id, "reason": "missing full-request measurement"})

        pack = trace.get("evidence_pack") or {}
        raw_refs = {source["source_ref"] for source in pack.get("raw_sources", [])}
        result_ids = {result["query_id"] for result in pack.get("results", [])}
        state = trace.get("state") or {}
        plan_ids = [query["id"] for query in state.get("plan", {}).get("queries", [])]
        for result in pack.get("results", []):
            if not set(result["source_refs"]) <= raw_refs:
                violations.append({"sample_id": sample_id, "reason": "result missing raw source",
                                   "query_id": result["query_id"]})
        for fact in pack.get("confirmed_facts", []):
            if not set(fact["source_refs"]) <= raw_refs:
                violations.append({"sample_id": sample_id, "reason": "fact missing raw source",
                                   "fact_id": fact["fact_id"]})
        if any(link.get("kind") != "CONFIRMED_BINDING" for link in pack.get("links", [])):
            violations.append({"sample_id": sample_id, "reason": "candidate relation in final pack"})
        accepted = {
            fact_id for query_id, uses in state.get("uses", {}).items()
            for fact_id, use in uses.items()
            if use["status"] == "ACCEPTED"
            and use["query_version"] == state["executions"][query_id]["version"]
            and use["binding_version"] == state["executions"][query_id]["binding_version"]
        }
        if not {fact["fact_id"] for fact in pack.get("supplemental_context", [])} <= accepted:
            violations.append({"sample_id": sample_id, "reason": "deferred fact leaked as supplemental"})
        for query_id in result_ids:
            if query_id not in plan_ids:
                violations.append({"sample_id": sample_id, "reason": "unknown result query"})

        old = parent[sample_id]
        flip = "STABLE" if old["answer_exact"] == row["answer_exact"] else "GAIN" if row["answer_exact"] else "LOSS"
        paired.append({
            "sample_id": sample_id,
            "question_type": row["question_type"],
            "flip": flip,
            "question": row["question"],
            "parent_prediction": old["prediction"],
            "candidate_prediction": row["prediction"],
            "parent_exact": old["answer_exact"],
            "candidate_exact": row["answer_exact"],
        })

    assert not metrics["violations"] and not violations
    comparison = {
        "correct_delta": metrics["correct"] - parent_metrics["correct"],
        "accuracy_delta": metrics["accuracy"] - parent_metrics["accuracy"],
        "f1_delta": metrics["f1"] - parent_metrics["f1"],
        "gains": [row["sample_id"] for row in paired if row["flip"] == "GAIN"],
        "losses": [row["sample_id"] for row in paired if row["flip"] == "LOSS"],
    }
    result = {
        "stage": "V08",
        "status": "ONE_RUN_COMPLETE",
        "candidate_run": str(args.candidate),
        "parent_run": str(args.parent),
        "candidate": metrics,
        "comparison": comparison,
        "final_evidence_audit": {
            "source_assessments": counters["SOURCE_ASSESSED"],
            "packs_built": counters["FINAL_EVIDENCE_PACK_BUILT"],
            "requests_measured": counters["FINAL_ANSWER_REQUEST_MEASURED"],
            "budget_failures": counters["FINAL_EVIDENCE_BUDGET_EXCEEDED"],
            "violations": violations,
        },
        "strict_stage_acceptance": "Not established: one fresh run was requested.",
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (args.report_dir / "paired_diff.csv").open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(paired[0]))
        writer.writeheader()
        writer.writerows(paired)
    print(json.dumps({
        "candidate": {key: metrics[key] for key in ("correct", "accuracy", "f1", "statuses", "runtime_statuses", "replayed_states")},
        "comparison": comparison,
        "final_evidence_audit": result["final_evidence_audit"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
