"""Conditional, isolated stage-3 continuation with frozen PLAN/UPDATE outputs.

Re-run the actual runtime from an empty store. Replay ONLY saved extraction
outputs for byte-identical text windows and the saved initial plan. RECALL,
MEMORY (including previously nonexistent downstream calls) and ANSWER are live.
No historical MEMORY output is substituted. No production default is changed.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_r2_memory_ab as ab
from scripts import evaluate_r2_memory_isolation as ex
from delaybind_core import prompts_r2 as prompts, subquery_runner_r2 as loop
from delaybind_core.data import load_records
from delaybind_core.runner import V5Runner
from delaybind_core.tokenization import TokenizerJSON

SAMPLES = ("dev_7920", "dev_4961", "dev_5343", "dev_91")


def stage3_messages(interface, payload):
    messages = prompts.messages(interface, payload)
    original = payload.get("original_memory_request", payload)
    if interface in {"MEMORY", "MEMORY_REPAIR"} and original.get("fact_only") and original.get("allowed_mode") == "BIND":
        assert prompts.MEMORY_FACT_ONLY_BIND in messages[1]["content"]
        messages[1]["content"] = messages[1]["content"].replace(prompts.MEMORY_FACT_ONLY_BIND, prompts.MEMORY_BIND_P17, 1)
    return messages


def stage3_version(interface, payload):
    original = payload.get("original_memory_request", payload)
    if interface in {"MEMORY", "MEMORY_REPAIR"} and original.get("fact_only") and original.get("allowed_mode") == "BIND":
        return "experiment-P17-full-frozen-plan-update"
    return prompts.prompt_version_for(interface, payload)


class FrozenReader:
    def __init__(self, calls, config, downstream_client=None):
        self.calls = calls
        self.config = config
        self.used = []
        self.downstream_client = downstream_client

    async def complete(self, *, interface, messages, **kwargs):
        # RECALL is a LOW-role interface in the real runner. Freeze extraction,
        # not the entire low agent; recall must remain a fresh model decision.
        if interface == "RECALL":
            assert self.downstream_client is not None, "Live RECALL client required"
            return await self.downstream_client.complete(interface=interface, messages=messages, **kwargs)
        assert interface == "UPDATE", "Frozen extraction must not invent repairs"
        assert len(self.used) < len(self.calls), "Extra input window"
        old = self.calls[len(self.used)]
        before = old["raw_request"]["messages"][1]["content"].split("Current window:\n", 1)[1]
        after = messages[1]["content"].split("Current window:\n", 1)[1]
        assert before == after, "Frozen UPDATE window changed"
        assert old.get("error") is None and isinstance(old["parsed_output"], str)
        self.used.append(dict(source_call_id=old["call_id"], window_sha256=ab.sha(after),
                              replayed_output=old["parsed_output"], new_request_sha256=ex.digest(messages)))
        return old["parsed_output"]


class DownstreamOnlyClient:
    def __init__(self, client):
        self.client, self.config = client, client.config

    async def complete(self, *, interface, **kwargs):
        assert interface in {"RECALL", "MEMORY", "MEMORY_REPAIR", "ANSWER"}, "PLAN and UPDATE are frozen"
        return await self.client.complete(interface=interface, **kwargs)


async def run(parent, env_file, output_name="stage3", recover_from=None):
    ex.assert_frozen(parent)
    ex.require_complete(parent, "2")
    summary = ab.read_json(parent / "summary.json")
    selected = ab.read_json(parent / "selection.json")["selected"]
    assert selected == "P17-full"
    assert summary["groups"]["2.heldout.P17-full"]["family_macro_accuracy"] > summary["groups"]["2.heldout.B"]["family_macro_accuracy"]
    assert Path(output_name).name == output_name
    output = parent / output_name
    assert not output.exists(), "New isolated chain run required; never overwrite"
    output.mkdir()
    previous = {}
    if recover_from:
        assert Path(recover_from).name == recover_from
        previous = {(r["sample_id"], r["version"]): r for r in ab.read_json(parent / recover_from / "summary.json")}
        for r in previous.values():
            assert r["status"] in {"ANSWERED", "INSUFFICIENT"} or r["reason_codes"] == ["Frozen extraction must not invent repairs"], "Only recover the documented harness routing failure"
    ab._load_env_file(env_file)
    source = ab.read_json(parent / "manifest.json")["source_manifest"]
    source_config = ab.read_json(Path(source["source_run"]) / "config.resolved.json")
    params = source["source_api_parameters"]
    source_config["api"].update(model_seed=params.get("seed"), max_output_tokens=params["max_tokens"])
    cfg = ab._api_config(source_config)
    tokenizer = TokenizerJSON(ROOT / "models/Qwen3.5-9B-tokenizer/tokenizer.json")
    samples = {s.sample_id: s for s in load_records(ROOT / "data/test/filtered_seed4_128/eval_2wikimultihopqa_50.json") if s.sample_id in SAMPLES}
    cases = ab.rows(parent / "regression_cases.jsonl")
    db = sqlite3.connect(f"file:{Path(source['source_run']) / 'experiment.sqlite'}?mode=ro", uri=True)
    frozen = {}
    for sid in SAMPLES:
        snapshot = next(c for c in cases if c["sample_id"] == sid and c["stage"] == 1)
        calls = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM model_calls WHERE run_id=? AND interface='UPDATE' ORDER BY rowid", (snapshot["source_run_id"],))]
        assert calls and not any("PLAN_HINT" in c["parsed_output"] for c in calls)
        frozen[sid] = dict(plan=snapshot["state"]["plan"], config=snapshot["state"]["run_metadata"]["config"], calls=calls)
    db.close()
    ab.write_json(output / "manifest.json", dict(
        samples=SAMPLES, versions=["B", "P17-full"], repetitions=1,
        purpose="Limited downstream integration, NOT eight-question EM or a new extraction benchmark",
        eligibility="P17-full improves heldout family-macro accuracy; known regression false bindings remain",
        production_default="B", update="Replay frozen outputs only after exact text-window match",
        plan="Frozen original plan", downstream="Real RECALL/MEMORY/activation/ANSWER, fresh stores",
        source_api_parameters=params, frozen_sha256=ex.digest(frozen), frozen=frozen,
        recover_from=recover_from,
        recovery_policy="Keep already completed trajectories; restart only trajectories aborted by the experiment RECALL routing bug. Preserve all original logs; never select a better answer.",
    ), exclusive=True)
    results = []
    for sid in SAMPLES:
        for version in ("B", "P17-full"):
            prior = previous.get((sid, version))
            if prior and prior["status"] in {"ANSWERED", "INSUFFICIENT"}:
                compact = {**prior, "carried_valid_run": str(parent / recover_from / f"{sid}-{version}.json")}
                results.append(compact)
                ab.write_json(output / "summary.json", results)
                print(json.dumps(compact, ensure_ascii=False), flush=True)
                continue
            store = ab.SQLiteEventStore(str(output / f"{sid}-{version}.sqlite"))
            client = ab.OpenAICompatibleClient(cfg, store=store)
            assert {k: v for k, v in client._request_payload("MEMORY", []).items() if k != "messages"} == params
            reader = FrozenReader(frozen[sid]["calls"], cfg, DownstreamOnlyClient(client))
            runner = V5Runner(DownstreamOnlyClient(client), reader_client=reader,
                              config=ab.RunnerConfig(**frozen[sid]["config"]), tokenizer=tokenizer)
            sample = samples[sid].model_copy(update={"answer": None, "answers": [], "supporting_facts": [], "evidences": []})
            # Scope monkeypatches to this standalone, sequential offline process.
            # No runner file/production symbol is persisted or modified.
            msg = stage3_messages if version == "P17-full" else prompts.messages
            ver = stage3_version if version == "P17-full" else prompts.prompt_version_for
            metadata_version = "experiment-P17-full-frozen-plan-update" if version == "P17-full" else prompts.MEMORY_BIND_PROMPT_VERSION
            try:
                with patch.object(loop, "messages", msg), patch.object(loop, "prompt_version_for", ver), patch.object(loop, "MEMORY_BIND_PROMPT_VERSION", metadata_version):
                    result = await loop.run_subqueries_r2(runner, run_id=f"stage3.{sid}.{version}", sample=sample,
                                                         manifest=None, store=store,
                                                         plan=ab.StateR2.model_validate(next(c["state"] for c in cases if c["sample_id"] == sid and c["stage"] == 1)).plan)
                assert len(reader.used) == len(reader.calls), "Frozen input stream not fully consumed"
                assert result["r2_metrics"]["transaction_replay_consistency"] == 1
                result.update(sample_id=sid, version=version, frozen_update_replays=reader.used)
                ab.write_json(output / f"{sid}-{version}.json", result, exclusive=True)
                calls = [c.model_dump(mode="json") for c in store.list_model_calls(f"stage3.{sid}.{version}")]
                compact = dict(sample_id=sid, version=version, status=result["status"], answer=result["answer"],
                               interface_calls=result["interface_calls"], reason_codes=result["reason_codes"],
                               transaction_replay_consistency=1, live_calls=len(calls),
                               live_interfaces=dict(Counter(c["interface"] for c in calls)),
                               cache_hits=sum(c["cache_hit"] for c in calls))
                results.append(compact)
                ab.write_json(output / "summary.json", results)
                print(json.dumps(compact, ensure_ascii=False), flush=True)
            finally:
                store.connection.close()
    ex.assert_frozen(parent)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=ex.DEFAULT_OUTPUT)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument("--output-name", default="stage3")
    parser.add_argument("--recover-from")
    args = parser.parse_args()
    asyncio.run(run(args.parent, args.env_file, args.output_name, args.recover_from))
