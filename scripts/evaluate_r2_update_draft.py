"""Four UPDATE-only requests for the reviewed P1-P5 draft; no production edits.

Two exact historical windows test key-fact recall. A split-sentence case is
tested as-is and with its own already-read document prefix restored. The
James Franklin case is excluded because the draft contains its answer.
No semantic retries, prompt revision, downstream calls, or accuracy judging
by another model. Raw outputs and predeclared targets are saved for review.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.cli import _api_config, _load_env_file
from delaybind_core.protocol_r2 import parse_update
from delaybind_core.storage import SQLiteEventStore


SOURCE = ROOT / "results/v52-r2-smoke16-member-graph-batch2"
PROMPT = ROOT / "experiments/update_p1p5_draft/prompt.txt"
VERSION = "update-p1p5-draft-1"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def save(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def prepare():
    draft = PROMPT.read_text(encoding="utf-8").rstrip()
    cases = []
    specs = [
        ("dev_2065", 1, "Q2: Sir James Power, 2nd Baronet was born on 6 December 1800.", True),
        ("dev_2955", 1, "Q2: Otakar Vávra was born in Hradec Králové.", True),
        ("dev_1850", 2, "Constantin J. David / Käthe von Nagy marriage: incomplete in this window; do not invent the missing subject.", False),
    ]
    for sid, number, target, score in specs:
        path, = (SOURCE / "trajectories").glob(f"*{sid}*.json")
        trajectory = json.loads(path.read_text())
        calls = [c for c in trajectory["model_calls"] if c["interface"] == "UPDATE"]
        call = calls[number - 1]
        snapshot, = [m for m in trajectory["context_manifests"]
                     if m.get("interface") == "UPDATE_SNAPSHOT" and m["context_id"] == call["context_id"]]
        source_messages = call["raw_request"]["messages"]
        body = source_messages[-1]["content"].split("Input data:\n", 1)[1]
        msgs = [dict(source_messages[0]), dict(role="user", content=
            "Protocol: " + VERSION + "\n" + draft + "\nInput data:\n" + body)]
        case = dict(case_id=f"{sid}.window{number}.exact", sample_id=sid, window_number=number,
                    kind="exact_historical_input", same_input_recall_case=score,
                    expected_key_evidence=target, source_trajectory=str(path.relative_to(ROOT)),
                    source_call_id=call["call_id"], source_run_id=call["run_id"],
                    source_request_parameters={k: v for k, v in call["raw_request"].items() if k != "messages"},
                    historical_output=call["raw_response"]["choices"][0]["message"]["content"],
                    input_sha256=sha(body), source_input_sha256=sha(body),
                    messages=msgs, parser_context=dict(context_id=snapshot["context_id"],
                        fact_only=True, query_graph=snapshot["query_graph"]))
        assert msgs[0] == source_messages[0]
        assert msgs[1]["content"].split("Input data:\n", 1)[1] == body
        cases.append(case)
        if sid == "dev_1850":
            first, = [m for m in trajectory["context_manifests"]
                      if m.get("interface") == "UPDATE_SNAPSHOT" and m["context_id"] == calls[0]["context_id"]]
            prefix, = [s["text"] for s in first["window_sources"] if s["source_ref"] == "D44@C0"]
            tail, = [s["text"] for s in snapshot["window_sources"] if s["source_ref"] == "D44@C1"]
            before, window = body.split("Current window:\n\n", 1)
            assert window.startswith(tail)
            restored = before + "Current window:\n\n" + prefix + window
            cases.append({**case, "case_id": "dev_1850.window2.same_document_prefix", "kind": "input_boundary_control",
                "expected_key_evidence": "Q2: Constantin J. David was married to Käthe von Nagy.",
                "input_sha256": sha(restored), "added_prefix": prefix,
                "messages": [dict(msgs[0]), dict(role="user", content=
                    "Protocol: " + VERSION + "\n" + draft + "\nInput data:\n" + restored)],
                "note": "Restores only D44@C0 before D44@C1. Not a same-input prompt comparison; no runtime change."})
    return cases


async def run(output):
    cases = prepare()
    config = json.loads((ROOT / "configs/v52_r2_smoke16_member_graph_batch2.json").read_text())
    _load_env_file(ROOT / ".env.local")
    api = _api_config(config)
    output.mkdir(parents=True, exist_ok=False)
    store = SQLiteEventStore(str(output / "experiment.sqlite"))
    client = OpenAICompatibleClient(api, store=store)
    for case in cases:
        request = client._request_payload("UPDATE", case["messages"])
        assert {k: v for k, v in request.items() if k != "messages"} == case["source_request_parameters"]
    prompt_before = sha((ROOT / "delaybind_core/prompts_r2.py").read_text())
    save(output / "cases.json", cases)
    save(output / "audit.json", dict(version=VERSION, started_at=datetime.now(timezone.utc).isoformat(),
        api={k: v for k, v in asdict(api).items() if k != "api_key"}, prompt_sha256=sha(PROMPT.read_text().rstrip()),
        production_prompt_sha256=prompt_before, live_interfaces=["UPDATE"], fresh_cache=True,
        semantic_retries=False, baseline="historical outputs, not newly resampled",
        excluded=dict(dev_8904="Draft contains the exact question's answer as an example.")))
    results = []
    for case in cases:
        print("START " + case["case_id"], flush=True)
        run_id = output.name + "-" + case["case_id"]
        raw, error, rejected, parsed = None, None, [], None
        try:
            raw = await client.complete(run_id=run_id, interface="UPDATE", agent_role="LOW", messages=case["messages"])
        except Exception as exc:
            error = str(exc)
        if raw is not None:
            try:
                update, rejected = parse_update(raw, case["parser_context"])
                parsed = update.model_dump(mode="json")
            except ValueError as exc:
                rejected = [dict(error=str(exc))]
        calls = [c.model_dump(mode="json") for c in store.list_model_calls(run_id)]
        result = dict(case_id=case["case_id"], run_id=run_id, raw_output=raw, api_error=error,
                      parsed_update=parsed, rejected=rejected, model_calls=calls)
        save(output / (case["case_id"] + ".json"), result)
        results.append(result)
        print(json.dumps({k: result[k] for k in ("case_id", "raw_output", "api_error", "rejected")}, ensure_ascii=False), flush=True)
    assert sha((ROOT / "delaybind_core/prompts_r2.py").read_text()) == prompt_before
    save(output / "results.json", results)
    store.connection.close()
    print("OUTPUT " + str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.prepare_only:
        print(json.dumps([{k: c[k] for k in ("case_id", "kind", "expected_key_evidence", "input_sha256")}
                          for c in prepare()], ensure_ascii=False, indent=2))
    else:
        output = args.output or ROOT / "results" / ("r2-update-p1p5-draft-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        asyncio.run(run(output.resolve()))
