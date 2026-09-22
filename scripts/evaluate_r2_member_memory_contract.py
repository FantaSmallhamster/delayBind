"""Isolated current-prompt MEMORY checks; no PLAN, UPDATE model, or ANSWER calls.

The default is offline: predeclared correct responses exercise the real parser
and state transitions, not model reasoning. Pass --live explicitly to obtain one
fresh MEMORY response per case from the configured service. No semantic retries
or protocol repairs conceal first-response errors in this diagnostic suite.
Every run requires a new output directory; historical benchmarks are untouched.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delaybind_core.api import ModelAPIError, OpenAICompatibleClient
from delaybind_core.archive import SentenceArchive
from delaybind_core.cli import _api_config, _load_config, _load_env_file
from delaybind_core.context_r2 import build_memory_context, build_update_context, persist_memory_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.navigation_r2 import assert_invariants, query_projection
from delaybind_core.prompts_r2 import messages, prompt_version_for
from delaybind_core.protocol_r2 import fact_alias_map, parse_memory, parse_update
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import EvidencePlanR2, fact_id
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.text_views_v52 import escaped

DEFAULT_CONFIG = ROOT / "configs/v52_r2_full128_member_graph_text20_5273a77.json"


@dataclass(frozen=True)
class Case:
    case_id: str
    query: str
    facts: tuple[str, ...]
    expected: dict[str, tuple[str, ...]]
    initial: dict[str, tuple[str, ...]] = field(default_factory=dict)


def cases() -> tuple[Case, ...]:
    direct = "Rowan is Mira's child."
    inverse = "Rowan's mother is Mira."
    wrong_entity = "Rowan is Nola's child."
    wrong_relation = "Rowan is Mira's teacher."
    query = "Who is Mira's child?"
    membership = "Who are the members of Cedar Circle?"
    alice = "Alice is a member of Cedar Circle."
    bob = "Bob is a member of Cedar Circle."
    return (
        Case("direct", query, (direct,), {"Rowan": (direct,)}),
        Case("inverse", query, (inverse,), {"Rowan": (inverse,)}),
        Case("wrong_entity", query, (wrong_entity,), {}),
        Case("wrong_relation", query, (wrong_relation,), {}),
        Case("correct_with_distractors", query,
             (wrong_entity, direct, wrong_relation), {"Rowan": (direct,)}),
        Case("multiple_members", membership, (alice, bob),
             {"Alice": (alice,), "Bob": (bob,)}),
        Case("rebind_keep", membership, ("Alice works as a physician.",),
             {"Alice": (alice,)}, initial={"Alice": (alice,)}),
        Case("rebind_add", membership, (bob,),
             {"Alice": (alice,), "Bob": (bob,)}, initial={"Alice": (alice,)}),
    )


def ingest(runtime: RuntimeR2, texts: tuple[str, ...]) -> None:
    """Freeze supplied facts through the normal ingestion contract, without LLMs."""
    payload = build_update_context(runtime, "Synthetic MEMORY contract check", [])
    update, errors = parse_update("\n".join("Q1 | " + escaped(t) for t in texts), payload)
    if errors:
        raise ValueError(f"Invalid fixture facts: {errors}")
    runtime.ingest(update, payload)


def memory_context(runtime: RuntimeR2):
    review = next(r for r in runtime.state.reviews.values()
                  if r.query_id == "Q1" and r.status not in {"DONE", "CANCELLED"})
    context = build_memory_context(runtime, review.review_id)
    persist_memory_context(runtime, context)
    return context


def canonical_response(expected: dict[str, tuple[str, ...]], context) -> str:
    aliases = {fid: alias for alias, fid in fact_alias_map(context.allowed_fact_ids).items()}
    return "\n".join(
        "BOUND | " + escaped(value) + " | " + ",".join(aliases[fact_id(t, [])] for t in support)
        for value, support in expected.items()
    ) or "NOOP"


def prepare(case: Case, store: SQLiteEventStore, config: RunnerConfig):
    plan = EvidencePlanR2(plan_id=case.case_id, queries=[
        dict(id="Q1", template=case.query, output="?person"),
        dict(id="Q2", template="Where was ?person born?", output="?place", inputs={"?person": "Q1"}),
    ])
    runtime = RuntimeR2(run_id=case.case_id, plan=plan,
                        archive=SentenceArchive(store, case.case_id), store=store, config=config)
    if case.initial:
        ingest(runtime, tuple(dict.fromkeys(t for ts in case.initial.values() for t in ts)))
        seed_context = memory_context(runtime)
        apply_memory(runtime, parse_memory(canonical_response(case.initial, seed_context), seed_context), seed_context)
    ingest(runtime, case.facts)
    context = memory_context(runtime)
    state = runtime.state
    old = state.binding_store.get(context.expected_binding_id)
    instance = next(q for q in query_projection(state)["queries"] if q["id"] == "Q1")
    payload = {**context.model_dump(mode="json"),
               "question": "Where were the members of Cedar Circle born?" if case.initial or
                   case.case_id == "multiple_members" else "Where was Mira's child born?",
               "query_instance": instance,
               "old_binding": old.model_dump(exclude={"source_refs", "decision_source_refs"}) if old else None,
               "staged_reviews": []}
    return runtime, context, payload


def final_members(runtime: RuntimeR2) -> list[dict]:
    binding_id = runtime.state.executions["Q1"].current_binding_id
    return [m.model_dump(mode="json") for m in runtime.state.binding_store[binding_id].members] if binding_id else []


def score(runtime: RuntimeR2, context, case: Case, raw: str) -> dict:
    result = {"parsed": None, "receipt": None, "protocol_error": None}
    try:
        parsed = parse_memory(raw, context)
        result["parsed"] = parsed.model_dump(mode="json")
        result["receipt"] = apply_memory(runtime, parsed, context)
        assert_invariants(runtime.state)
    except ValueError as exc:
        result["protocol_error"] = str(exc)
    result["final_members"] = final_members(runtime)
    expected = {value: sorted(fact_id(t, []) for t in support) for value, support in case.expected.items()}
    actual = {m["value"]: sorted(m["direct_fact_ids"]) for m in result["final_members"]}
    result["state_matches"] = actual == expected and len(actual) == len(result["final_members"])
    result["passed"] = result["protocol_error"] is None and result["state_matches"]
    result["downstream_binding_id"] = runtime.state.executions["Q2"].current_binding_id
    return result


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


async def run(output: Path, *, config_path: Path = DEFAULT_CONFIG,
              env_file: Path = ROOT / ".env.local", live: bool = False) -> dict:
    config_data = _load_config(config_path)
    config = RunnerConfig.from_mapping(config_data.get("runner") or config_data)
    if config.protocol_version != "v5.2-r2" or config.sentence_splitting:
        raise ValueError("This suite requires R2 fact-only mode (sentence_splitting=false).")
    api_config = None
    if live:
        _load_env_file(env_file)
        api_config = _api_config({**config_data, "api": {
            **config_data.get("api", {}), **config_data.get("high_api", {})}})
    output.mkdir(parents=True, exist_ok=False)
    store = SQLiteEventStore(str(output / "runtime_and_calls.sqlite"))
    client = OpenAICompatibleClient(api_config, store=store) if live else None
    records = []
    try:
        write_json(output / "manifest.json", {
            "mode": "live" if live else "offline_fixture",
            "config_path": str(config_path.resolve()),
            "runner_config": asdict(config),
            "api": {k: v for k, v in asdict(api_config).items() if k != "api_key"} if live else None,
            "note": "Offline fixture responses test transitions only; they are not model accuracy results.",
            "model_calls": "One MEMORY request per case; transport retries follow existing config; no protocol repairs.",
            "cases": [asdict(case) for case in cases()],
        })
        for case in cases():
            runtime, context, payload = prepare(case, store, config)
            request = messages("MEMORY", payload)
            record = {"case": asdict(case), "payload": payload, "messages": request,
                      "prompt_version": prompt_version_for("MEMORY", payload),
                      "expected_mode": "REBIND" if case.initial else "BIND",
                      "context_mode": context.allowed_mode,
                      "raw_output": None, "api_error": None, "score": None}
            try:
                raw = await client.complete(run_id=case.case_id, interface="MEMORY", messages=request,
                                            agent_role="HIGH") if live else (
                    "NOOP" if case.case_id == "rebind_keep" else canonical_response(case.expected, context))
                record["raw_output"] = raw
                record["score"] = score(runtime, context, case, raw)
            except ModelAPIError as exc:
                record["api_error"] = str(exc)
            record["mode_matches"] = record["context_mode"] == record["expected_mode"]
            record["passed"] = record["mode_matches"] and bool((record["score"] or {}).get("passed"))
            records.append(record)
            write_json(output / f"{case.case_id}.json", record)
            print(f"{case.case_id}: {'PASS' if record['passed'] else 'FAIL'}", flush=True)
        summary = {"mode": "live" if live else "offline_fixture", "total": len(records),
                   "passed": sum(r["passed"] for r in records),
                   "api_errors": sum(r["api_error"] is not None for r in records),
                   "protocol_errors": sum(bool((r["score"] or {}).get("protocol_error")) for r in records),
                   "state_mismatches": sum(r["score"] is not None and not r["score"]["state_matches"] for r in records),
                   "output": str(output.resolve())}
        write_json(output / "summary.json", summary)
        return summary
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly call the configured real API.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = args.output or ROOT / "results" / f"r2-member-memory-contract-{'live' if args.live else 'offline'}-{stamp}"
    summary = asyncio.run(run(output, config_path=args.config, env_file=args.env_file, live=args.live))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(0 if summary["passed"] == summary["total"] else 1)


if __name__ == "__main__":
    main()
