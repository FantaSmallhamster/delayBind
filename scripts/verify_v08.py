"""Offline checks for the slim V08 package, frozen dataset and retained runs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import string
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness
from delaybind_core.metrics import answer_exact, answer_token_f1
from delaybind_core.replay import replay_events
from delaybind_core.schema import QueryPlan, RuntimeEvent
from delaybind_core.subqueries import initial_subquery_state
from delaybind_core.subquery_runner import parse_final_answer
from scripts.ultimate.run_direct_raw_context import TEMPLATE, prompt

DATA = ROOT / "data/test/filtered_seed4_128_refresh_v08"
RUNS = {
    "v08": "v08_refreshed_benchmark_full128_r1",
    "direct": "direct_raw_refreshed_full128_t0_r1",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def official_normalize(answer):
    """Original 2Wiki EM normalization, used for screening records only."""
    text = str(answer).lower()
    text = "".join(c for c in text if c not in string.punctuation)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def verify_dataset():
    frozen = read(DATA / "frozen_manifest.json")
    for name, digest in frozen["outputs"].items():
        require(sha(DATA / name) == digest, f"Frozen dataset changed: {name}")
    records = read(DATA / "eval_2wikimultihopqa_50.json")
    official = read(DATA / "selected_official.json")
    ids = [r["id"] for r in records]
    require(len(ids) == len(set(ids)) == 128, "Expected 128 unique questions")
    require(ids == frozen["benchmark_ids"], "Question order changed")
    require(dict(Counter(r["type"] for r in records)) == frozen["type_counts"], "Type counts changed")
    require(len(official) == 128, "Original records missing")
    official_by_id = {r["_id"]: r for r in official}
    require(set(official_by_id) == {r["official_id"] for r in records}, "Original IDs mismatch")
    for row in records:
        original = official_by_id[row["official_id"]]
        require(row["input"].strip() == original["question"].strip(), "Original question mismatch")
        docs = re.split(r"^Document \d+:\n", row["context"], flags=re.M)[1:]
        require(len(docs) == row["num_docs"] == 50, f"Document count: {row['id']}")
        require(all(0 <= idx < 50 for idx in row["evidence_idx"]), "Evidence index out of range")
    provenance = ROOT / "reports/dataset_refresh_v08"
    # frozen.screening_answers_sha256 describes the full historical candidate pool.
    # The retained 120-response subset has its own digest in the cleanup manifest.
    for name, field in [("new_candidate_evidence_review.json", "human_review_sha256"),
                        ("existing_evidence_audit.json", "original_evidence_audit_sha256")]:
        require(sha(provenance / name) == frozen[field], f"Provenance changed: {name}")
    answers = read(provenance / "selected_no_context_answers.json")
    require(len(answers) == len({r["request_id"] for r in answers}) == 120, "Expected 120 independent screening responses")
    grouped = defaultdict(list)
    for row in answers:
        require(row["valid"] and row["finish_reason"] == "stop" and row["model"] == "Qwen/Qwen3.5-9B", "Invalid screening response")
        require(all(official_normalize(row["answer"]) != official_normalize(g) for g in row["answers"]), "Screening answer was correct")
        grouped[row["sample_id"]].append(row)
    require(set(grouped) == set(frozen["added_ids"]), "Screening IDs mismatch")
    for sid, trials in grouped.items():
        require(len(trials) == 4 and {r["trial_index"] for r in trials} == {0, 1, 2, 3}, f"Four screening trials missing: {sid}")
    reviews = {r["sample_id"]: r for r in read(provenance / "new_candidate_evidence_review.json")}
    require(all(reviews[sid]["verdict"] == "ACCEPT" for sid in grouped), "Selected candidate not accepted")
    config = ExperimentConfig.from_mapping(read(ROOT / "configs/ultimate/v08_refreshed_benchmark_full128_r1.json"))
    samples = ExperimentHarness(config, client_factory=lambda store: None)._samples()
    require([s.sample_id for s in samples] == ids, "Experiment loader changed question selection")
    return records, frozen


def verify_v08_trace(trace, row):
    states = {}
    for call in trace["model_calls"]:
        interface = call["interface"]
        require(interface in {"PLAN", "UPDATE", "MEMORY_GROUNDED", "RECALL", "ANSWER"}, "Unexpected interface")
        require(call["agent_role"] == ("HIGH" if interface in {"PLAN", "MEMORY_GROUNDED"} else "LOW"), "Agent routing changed")
    for event in trace["events"]:
        kind, payload = event["event_type"], event["payload"]
        if kind == "PLAN_CREATED":
            initial = initial_subquery_state(QueryPlan.model_validate(payload["plan"]))
            states = {qid: execution.model_dump(mode="json") for qid, execution in initial.executions.items()}
        elif kind == "QUERY_STATE_UPDATED":
            states[payload["query_id"]] = payload["state"]
        elif kind in {"FACT_COMMITTED", "FACT_DEFERRED"} and payload.get("reason") != "PLAN_REROUTE":
            live = states[payload["use"]["query_id"]]["status"]
            require(kind == ("FACT_DEFERRED" if live == "DORMANT" else "FACT_COMMITTED"), "Fact routing mismatch")
            require(payload.get("routing_source") == "QUERY_STATE" and payload.get("query_status") == live, "Routing provenance mismatch")
        elif kind == "MEMORY_VERIFY_COMPLETED":
            require(payload.get("mode") == "joint_source_memory", "Unexpected memory mode")
        elif kind == "FINAL_ANSWER_REQUEST_MEASURED":
            require(payload["limit"] is None or payload["size"] <= payload["limit"], "Answer budget exceeded")
    state = trace.get("state") or {}
    if state:
        replay = replay_events(RuntimeEvent.model_validate(e) for e in trace["events"])
        require(replay.export() == state, f"State replay mismatch: {row['sample_id']}")
    if row["status"] == "OK":
        kinds = {e["event_type"] for e in trace["events"]}
        require({"FINAL_EVIDENCE_PACK_BUILT", "FINAL_ANSWER_REQUEST_MEASURED"} <= kinds, "Final evidence events missing")
    pack = trace.get("evidence_pack") or {}
    refs = {s["source_ref"] for s in pack.get("raw_sources", [])}
    for item in pack.get("results", []) + pack.get("confirmed_facts", []):
        require(set(item["source_refs"]) <= refs, "Final pack lacks cited original text")
    require(all(link.get("kind") == "CONFIRMED_BINDING" for link in pack.get("links", [])), "Unconfirmed link in final pack")
    accepted = {fid for qid, uses in state.get("uses", {}).items() for fid, use in uses.items()
                if use["status"] == "ACCEPTED"
                and use["query_version"] == state["executions"][qid]["version"]
                and use["binding_version"] == state["executions"][qid]["binding_version"]}
    require({f["fact_id"] for f in pack.get("supplemental_context", [])} <= accepted, "Unaccepted supplemental fact")
    return bool(state)


def verify_runs(records, frozen):
    by_id = {r["id"]: r for r in records}
    checks = {}
    for kind, name in RUNS.items():
        run = ROOT / "results/ultimate" / name
        rows = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
        require(len(rows) == len({r["sample_id"] for r in rows}) == 128, f"Result count: {name}")
        require({r["sample_id"] for r in rows} == set(by_id), "Run question IDs mismatch")
        manifest = read(run / "code_manifest.json")
        config = read(ROOT / "configs/ultimate" / (name + ".json"))
        require(manifest["sample_ids"] == [r["id"] for r in records], "Run input order mismatch")
        require(manifest["data_sha256"] == frozen["outputs"]["eval_2wikimultihopqa_50.json"], "Run dataset digest mismatch")
        require(manifest["tokenizer_sha256"] == sha(ROOT / config["tokenizer_path"]), "Tokenizer changed")
        require(len(list((run / "trajectories").glob("*.json"))) == 128, "Trajectory count mismatch")
        replays = 0
        for row in rows:
            original = by_id[row["sample_id"]]
            require(row["question"] == original["input"] and row["gold_answers"] == original["answers"] and row["question_type"] == original["type"], "Run question or gold mismatch")
            require(row["answer_exact"] == answer_exact(row["prediction"], original["answers"]), "EM mismatch")
            require(abs(row["answer_token_f1"] - answer_token_f1(row["prediction"], original["answers"])) < 1e-12, "F1 mismatch")
            trace = read(run / "trajectories" / Path(row["trajectory_path"]).name)
            if kind == "v08":
                require((row["method"], row["order"]) == ("v5_predicted", "original"), "Unexpected V08 condition")
                require(trace["sample_id"] == row["sample_id"] and trace["run_id"] == row["run_id"], "Trajectory identity mismatch")
                replays += verify_v08_trace(trace, row)
            for call in trace["model_calls"]:
                request = call["raw_request"]
                require(request["model"] == "Qwen/Qwen3.5-9B" and request["temperature"] == 0, "Model/sampling mismatch")
                require(request["top_p"] == .95 and request["seed"] == 4 and request["max_tokens"] == 4096 and request["enable_thinking"] is False, "Sampling parameters changed")
            if kind == "direct":
                require(len(trace["model_calls"]) == 1, "Direct run had multiple requests")
                call = trace["model_calls"][0]
                require(not call["cache_hit"] and not call.get("error"), "Direct request failed or used cache")
                require(call["interface"] == "ANSWER" and call["agent_role"] == "LOW", "Direct interface mismatch")
                require(call["raw_request"]["messages"] == [{"role": "user", "content": prompt(original)}], "Direct context truncated or modified")
                require("response_format" not in call["raw_request"], "Direct run used structured output")
                require(parse_final_answer(call["parsed_output"], "boxed").answer == row["prediction"], "Direct parsed answer mismatch")
        if kind == "direct":
            require(manifest["prompt_template"] == TEMPLATE and manifest["script_sha256"] == sha(ROOT / "scripts/ultimate/run_direct_raw_context.py"), "Direct prompt/script changed")
        stats = {"count": len(rows), "correct": sum(r["answer_exact"] for r in rows),
                 "accuracy": sum(r["answer_exact"] for r in rows) / len(rows),
                 "f1": sum(r["answer_token_f1"] for r in rows) / len(rows),
                 "statuses": dict(Counter(r["status"] for r in rows)), "replayed_states": replays}
        stats["by_type"] = {t: {"count": sum(r["question_type"] == t for r in rows),
                                      "correct": sum(r["answer_exact"] for r in rows if r["question_type"] == t)} for t in frozen["type_counts"]}
        checks[kind] = stats
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records, frozen = verify_dataset()
    runs = verify_runs(records, frozen)
    cleanup = read(ROOT / "reports/v08_cleanup/manifest.json")
    for name in RUNS.values():
        historical = read(ROOT / "results/ultimate" / name / "code_manifest.json")
        require(historical["source_sha256"] == cleanup["historical_baseline_core_sha256"], "Historical code baseline mismatch")
    for name, digest in cleanup["current_core_sha256"].items():
        require(sha(ROOT / name) == digest, f"Code changed after cleanup: {name}")
    for name, digest in cleanup["retained_artifact_sha256"].items():
        require(sha(ROOT / name) == digest, f"Retained artifact changed: {name}")
    for name in cleanup["removed_core_files"]:
        require(not (ROOT / name).exists(), f"Obsolete module restored: {name}")
    result = {"status": "PASS", "verification": "offline; no new model requests",
              "dataset_sha256": frozen["outputs"]["eval_2wikimultihopqa_50.json"],
              "samples": 128, "screening_responses": 120, "runs": runs,
              "cleanup_manifest_verified": True,
              "historical_source_manifests": "Preserved as recorded at evaluation time; not rewritten after cleanup."}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
