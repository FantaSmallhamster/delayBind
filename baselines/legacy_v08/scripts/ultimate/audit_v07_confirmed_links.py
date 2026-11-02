"""Audit the V07 confirmed-binding relationship run."""

import argparse
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

    relation_violations = []
    confirmed_links = 0
    candidate_links = 0
    support_refreshes = 0
    paired = []
    for sample_id in manifest["sample_ids"]:
        row = candidate[sample_id]
        trace = read_json(args.candidate / "trajectories" / Path(row["trajectory_path"]).name)
        query_states = {}
        for event in trace["events"]:
            kind, payload = event["event_type"], event["payload"]
            if kind == "QUERY_STATE_UPDATED":
                query_states[payload["query_id"]] = payload["state"]
            elif kind == "DEPENDENT_SUPPORT_UPDATED":
                support_refreshes += 1
            elif kind == "BINDING_LINK_CREATED":
                link = payload["link"]
                if link.get("kind") != "CONFIRMED_BINDING":
                    candidate_links += 1
                    relation_violations.append({"sample_id": sample_id, "reason": "legacy candidate link"})
                    continue
                confirmed_links += 1
                state = query_states.get(link["query_id"], {})
                expected = (
                    state.get("status") == "RESOLVED"
                    and state.get("result") == link.get("value")
                    and sorted(state.get("support_fact_ids", [])) == sorted(link.get("support_fact_ids", []))
                    and state.get("version") == link.get("query_version")
                    and state.get("binding_version") == link.get("binding_version")
                    and state.get("resolved_version") == link.get("resolved_version")
                    and bool(link.get("rendered_query"))
                    and bool(link.get("source_refs"))
                )
                if not expected:
                    relation_violations.append({"sample_id": sample_id, "reason": "link does not match bound state",
                                                "query_id": link.get("query_id")})
        for link in trace["state"].get("links", {}).values():
            if link.get("kind") != "CONFIRMED_BINDING":
                relation_violations.append({"sample_id": sample_id, "reason": "non-confirmed final link"})

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

    assert not metrics["violations"] and not relation_violations
    comparison = {
        "correct_delta": metrics["correct"] - parent_metrics["correct"],
        "accuracy_delta": metrics["accuracy"] - parent_metrics["accuracy"],
        "f1_delta": metrics["f1"] - parent_metrics["f1"],
        "gains": [row["sample_id"] for row in paired if row["flip"] == "GAIN"],
        "losses": [row["sample_id"] for row in paired if row["flip"] == "LOSS"],
    }
    result = {
        "stage": "V07",
        "status": "ONE_RUN_COMPLETE",
        "candidate_run": str(args.candidate),
        "parent_run": str(args.parent),
        "candidate": metrics,
        "comparison": comparison,
        "relation_audit": {
            "confirmed_links": confirmed_links,
            "candidate_links": candidate_links,
            "support_refreshes": support_refreshes,
            "violations": relation_violations,
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
        "relation_audit": result["relation_audit"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
