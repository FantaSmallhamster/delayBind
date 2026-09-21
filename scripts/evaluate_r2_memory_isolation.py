"""Repeated, cache-independent MEMORY ablations; production stays unchanged.

prepare -> run --stage 1 -> diagnose -> run --stage diagnostic -> select
-> run --stage 2 -> report. Every condition has three fixed repeats. API-only
failures may be resumed; a received model response is never replaced.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import evaluate_r2_memory_ab as ab
from delaybind_core import prompts_r2 as prompts
from delaybind_core.memory_experiments_r2 import experimental_messages, experimental_view, validate_mapping
from delaybind_core.text_protocol_v52 import fields, refs
from delaybind_core.text_views_v52 import escaped
from delaybind_core.schema_v52 import QueryPlanV3
from delaybind_core.context_r2 import build_update_context
from delaybind_core.protocol_r2 import parse_update

spec = importlib.util.spec_from_file_location("isolation_annotations", ab.FIXTURES / "isolation_annotations.py")
labels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labels)
DEFAULT_OUTPUT = ROOT / "results/r2-memory-isolation-p17-20260921"
HELDOUT_SOURCE = ROOT / "results/v51-2wiki50-seed4-full128-node199-protocol-v4-retry5/experiment.sqlite"
FREEZE_NAMES = ("SYSTEM_FACT_ONLY_BIND", "MEMORY_FACT_ONLY_BIND", "MEMORY_FACT_ONLY_NEUTRAL",
                "MEMORY_BIND_P17", "MEMORY_CANDIDATE_DIAGNOSTIC")
FREEZE_FILES = ("delaybind_core/memory_experiments_r2.py", "delaybind_core/subquery_runner_r2.py",
                "tests/fixtures/r2_memory_ab/isolation_annotations.py", "scripts/evaluate_r2_memory_ab.py")


def digest(value):
    return ab.sha(json.dumps(value, ensure_ascii=False, sort_keys=True))


def family(case):
    return case["sample_id"]


def ordered(case, texts, aliases=None):
    """Change order without renumbering; optional aliases change IDs only."""
    by_text = {f["text"]: f for f in case["eligible_facts"]}
    assert set(texts) == set(by_text) and len(texts) == len(by_text)
    result = [copy.deepcopy(by_text[t]) for t in texts]
    if aliases is not None:
        assert set(aliases) == set(texts)
        for f in result:
            f["short_id"] = aliases[f["text"]]
    validate_mapping(case, result)
    return result


def preserve_aliases(base, variant, replacements=None):
    """Noise gets fresh IDs after originals; replacing wording inherits its ID."""
    replacements = replacements or {}
    aliases = {replacements.get(f["text"], f["text"]): f["short_id"] for f in base["eligible_facts"]}
    next_id = len(aliases) + 1
    for f in variant["eligible_facts"]:
        if f["text"] not in aliases:
            aliases[f["text"]] = f"F{next_id}"
            next_id += 1
        f["short_id"] = aliases[f["text"]]
    variant["short_to_durable"] = {f["short_id"]: f["durable_id"] for f in variant["eligible_facts"]}
    validate_mapping(variant, variant["eligible_facts"])
    return variant


def changed(base, kind, texts, support, *, value=None, noop=False, query=None, replacements=None):
    c = ab.perturb(base, kind, texts, support, decision="NOOP" if noop else "BOUND", query_text=query)
    if not noop and value is not None:
        c["expected"]["value"] = value
    # This is explicit synthetic data, not a reconstructed historical request.
    c["provenance"] = {"kind": "controlled_perturbation", "parent": base["case_id"]}
    return preserve_aliases(base, c, replacements) if len(texts) >= len(base["eligible_facts"]) else c


def make_condition(case, condition, version, stage, *, group="core", **options):
    facts = options.get("facts", case["eligible_facts"])
    messages = experimental_messages(case, version, **options)
    return dict(condition_id=condition, case_id=case["case_id"], family=family(case),
                stage=stage, group=group, version=version, options=options,
                messages=messages, message_sha256=digest(messages),
                short_to_durable={f["short_id"]: f["durable_id"] for f in facts},
                expected=case["expected"], repeats=labels.REPEATS)


def save_lines(path, rows):
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def freeze():
    return dict(legacy=ab.assert_frozen_contracts(),
                constants={name: ab.sha(getattr(prompts, name)) for name in FREEZE_NAMES},
                files={name: ab.sha((ROOT / name).read_text()) for name in FREEZE_FILES})


def assert_frozen(output):
    manifest = ab.read_json(output / "manifest.json")
    assert freeze() == manifest["frozen"], "Frozen production/experiment contract changed"
    for name, expected in manifest["artifact_sha256"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == expected, name
    return manifest


def new_projection(*, case_id, question, query, output_variable, texts, value, support):
    """Load saved fact text into a disposable single-query R2 fixture, no LLM."""
    store = ab.SQLiteEventStore()
    config = ab.RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False)
    plan = QueryPlanV3(plan_id=case_id, queries=[dict(id="Q1", template=query, output=output_variable)])
    runtime = ab.RuntimeR2(run_id=case_id, plan=plan, archive=ab.SentenceArchive(store, case_id), store=store, config=config)
    runtime.state.run_metadata = {"config": asdict(config)}
    payload = build_update_context(runtime, question, [])
    update, rejected = parse_update("\n".join("Q1 | " + escaped(t) for t in texts), payload)
    assert not rejected
    runtime.ingest(update, payload)
    ctx = ab.build_memory_context(runtime, next(iter(runtime.state.reviews)))
    expected = dict(decision="NOOP" if value is None else "BOUND", value=value,
                    support_fact_ids=[ab.fact_id(texts[i], []) for i in support],
                    annotation_basis="visible_facts_only", reason="Predeclared held-out visible-input label")
    case = ab.make_case(case_id=case_id, state=runtime.state, ctx=ctx, question=question,
                        expected=expected, order=texts, run_id=case_id, stage=2)
    store.connection.close()
    return case


def heldout_cases():
    db = sqlite3.connect(f"file:{HELDOUT_SOURCE}?mode=ro", uri=True)
    cases = []
    for row, sid, qid, value, support in labels.HELDOUT:
        call = json.loads(db.execute("SELECT payload_json FROM model_calls WHERE rowid=?", (row,)).fetchone()[0])
        assert sid in call["run_id"] and call["interface"] == "MEMORY"
        text = call["raw_request"]["messages"][0]["content"]
        question = text.split("Question:\n", 1)[1].split("\n\nPlan:", 1)[0]
        match = re.search(rf"^{qid} \[ACTIVE\] (.+)\n  output: (\?\w+); depends_on: NONE", text, re.M)
        assert match, (sid, qid)
        rows = text.split("Working memory:\n", 1)[1].split("\n\nSaved plan hints:", 1)[0].splitlines()
        facts = [line.split(" | ", 2)[2] for line in rows]
        case = new_projection(case_id=f"{sid}.heldout", question=question, query=match[1], output_variable=match[2],
                              texts=facts, value=value, support=support)
        case["provenance"] = dict(kind="historical_v51_facts_r2_single_query_projection", database=str(HELDOUT_SOURCE),
                                  rowid=row, source_call_id=call["call_id"], historical_fact_lines=rows,
                                  warning="Not a native R2 snapshot; historical model output and final gold unused")
        cases.append(case)
    db.close()
    by_sid = {c["sample_id"]: c for c in cases}
    # Three new families get order, ID-only and noise stability conditions.
    for sid in ("dev_6489", "dev_6579", "dev_11816"):
        base = by_sid[sid]
        for kind in ("order", "ids", "noise"):
            if kind == "noise":
                texts = [f["text"] for f in base["eligible_facts"]]
                support = [f["text"] for f in base["eligible_facts"] if f["durable_id"] in base["expected"]["support_fact_ids"]]
                case = changed(base, "noise", ["The unrelated film Silver Harbor was directed by Nola Reed.", *texts], support)
            else:
                case = copy.deepcopy(base)
                case["case_id"] += "." + kind
                case["parent_case_id"] = base["case_id"]
                if kind == "order":
                    case["eligible_facts"].reverse()
                else:
                    aliases = list(reversed([f["short_id"] for f in case["eligible_facts"]]))
                    for f, alias in zip(case["eligible_facts"], aliases):
                        f["short_id"] = alias
            case["stability_parent"] = base["case_id"]
            cases.append(case)
    # Role composition uses ONLY the two historical facts, with synthetic queries.
    base = by_sid["dev_3867"]
    ts = [f["text"] for f in base["eligible_facts"]]
    cases += [changed(base, "parent_inverse", ts, [ts[0]], value="Masinissa", query="Who is Gulussa's parent?"),
              changed(base, "father_two_facts", ts, ts, value="Masinissa", query="Who is Gulussa's father?"),
              changed(base, "father_role_missing", [ts[0]], [], noop=True, query="Who is Gulussa's father?")]
    # SINGLE title/name pair, explicit wrong relation, counterevidence, true plurality.
    base = by_sid["dev_9001"]
    original = base["eligible_facts"][0]["text"]
    bare = original.replace("George Compton, 4th Earl of Northampton", "George Compton")
    cases.append(changed(base, "bare_name", [bare], [bare], value="George Compton", replacements={original: bare}))
    cases[-1]["expected"]["accepted_values"] = ["George Compton"]
    base["expected"]["accepted_values"] = ["George Compton, 4th Earl of Northampton", "George Compton"]
    base = by_sid["dev_4741"]
    original = base["eligible_facts"][0]["text"]
    for kind, extra, noop in [
        ("wrong_relation", "War of the Buttons (1994 Film) was produced by Mira Stone.", False),
        ("counterevidence", "John Roberts did not direct War of the Buttons (1994 Film).", True),
        ("multiple_entities", "War of the Buttons (1994 Film) was also directed by Mira Stone.", True),
    ]:
        cases.append(changed(base, kind, [original, extra], [] if noop else [original], noop=noop))
    # A single work title with 'and', plus a genuinely plural pair. Synthetic,
    # unrelated to old eight questions; no punctuation heuristic is introduced.
    for kind, text, value in [
        ("single_work_and", "Nola Reed directed the film Cedar and Stone.", "Cedar and Stone"),
        ("two_works_and", "Nola Reed directed the two films Cedar and Stone.", None),
    ]:
        case = new_projection(case_id="synthetic_works." + kind, question="Where was the director of Cedar and Stone born?",
                              query="Which film did Nola Reed direct?", output_variable="?film", texts=[text],
                              value=value, support=[] if value is None else [0])
        case["provenance"] = dict(kind="fully_synthetic_single_vs_multiple_control")
        cases.append(case)
    return cases


def prepare(output):
    assert not output.exists(), "Use a new output directory"
    old_cases = ab.rows(ab.DEFAULT_OUTPUT / "cases.jsonl")
    old = {c["case_id"]: copy.deepcopy(c) for c in old_cases}
    core_ids = ["dev_4961.Q1.2", "dev_4961.Q1.2.reverse_order", "dev_4961.Q1.2.equivalent_expression",
                "dev_4961.Q1.2.wrong_entity", "dev_4961.Q1.2.wrong_relation", "dev_5343.Q1.1",
                "dev_7920.Q1.2", "dev_7920.Q1.2.reverse_order", "dev_10616.Q1.1",
                "dev_10616.Q1.1.parent_inverse", "dev_10616.Q1.1.father_with_role_evidence",
                "dev_10616.Q1.1.female_parent_guard", "dev_4961.Q1.1", "dev_7920.Q1.1",
                "dev_4784.Q1.1", "dev_954.Q1.1"]
    # Repair old perturbation ID confounds in NEW experiment cases only.
    focused = []
    for cid in core_ids:
        c = old[cid]
        if cid.startswith("dev_4961.Q1.2."):
            base = old["dev_4961.Q1.2"]
            replacements = {}
            if c["perturbation"] == "equivalent_expression":
                replacements[base["eligible_facts"][1]["text"]] = "Film Yamata was directed by Alexander Korda."
            preserve_aliases(base, c, replacements)
        focused.append(c)
    title = old["dev_5343.Q1.1"]
    title["expected"]["accepted_values"] = [title["expected"]["value"], "Richard of Shrewsbury"]
    original = title["eligible_facts"][0]["text"]
    bare = original.replace("Richard of Shrewsbury, 1st Duke of York", "Richard of Shrewsbury")
    focused.append(changed(title, "bare_name", [bare], [bare], value="Richard of Shrewsbury", replacements={original: bare}))
    focused[-1]["expected"]["accepted_values"] = ["Richard of Shrewsbury"]
    role = old["dev_10616.Q1.1"]
    son = role["eligible_facts"][0]["text"]
    direct = "Jobst of Limburg is Georg Ernst of Limburg Stirum's father."
    mother = "Jobst of Limburg is Georg Ernst of Limburg Stirum's mother."
    focused += [changed(role, "father_direct", [direct], [direct], value="Jobst of Limburg", replacements={son: direct}),
                changed(role, "mother_explicit", [son, mother], [], noop=True)]
    conditions = []
    for case in focused:
        for version in ("B", "P17-full", "P17-local"):
            # The P17-local role suite has no global question or semantic output.
            conditions.append(make_condition(case, "core." + case["case_id"], version, "1"))
    for cid in ("dev_5343.Q1.1", "dev_5343.Q1.1.bare_name", "dev_4961.Q1.2",
                "dev_4961.Q1.2.equivalent_expression", "dev_7920.Q1.2", "dev_10616.Q1.1.parent_inverse"):
        case = next(c for c in focused if c["case_id"] == cid)
        for version in ("A", "A-neutral"):
            conditions.append(make_condition(case, "core." + cid, version, "1", group="neutral"))
    for cid in ("dev_10616.Q1.1.parent_inverse", "dev_4961.Q1.2"):
        for tag, hide, neutral in (("D10", True, False), ("D01", False, True), ("D11", True, True)):
            conditions.append(make_condition(old[cid], tag + "." + cid, "B", "1", group="display",
                                             hide_question=hide, neutral_output=neutral))
    base = old["dev_4961.Q1.2"]
    wrong, right = [f["text"] for f in base["eligible_facts"]]
    for tag, order in (("I01", [wrong, right]), ("I11", [right, wrong])):
        conditions.append(make_condition(base, tag + "." + base["case_id"], "B", "1", group="id_order",
                                         facts=ordered(base, order, {wrong: "F2", right: "F1"})))
    lexical = wrong.replace("Fanny and Alexander", "Silver Harbor")
    case = changed(base, "lexical_control", [lexical, right], [right], replacements={wrong: lexical})
    focused.append(case)
    conditions.append(make_condition(case, "lexical." + case["case_id"], "B", "1", group="lexical"))
    heldout = heldout_cases()
    assert not ({c["sample_id"] for c in old_cases} & {c["sample_id"] for c in heldout})
    # Store old regression cases unchanged separately; focused copies may fix IDs.
    output.mkdir(parents=True)
    save_lines(output / "stage1_cases.jsonl", focused)
    save_lines(output / "regression_cases.jsonl", old_cases)
    save_lines(output / "heldout_cases.jsonl", heldout)
    save_lines(output / "stage1_conditions.jsonl", conditions)
    artifacts = ("stage1_cases.jsonl", "regression_cases.jsonl", "heldout_cases.jsonl", "stage1_conditions.jsonl")
    manifest = dict(experiment="MEMORY input isolation, no production changes", frozen=freeze(),
                    artifact_sha256={n: hashlib.sha256((output / n).read_bytes()).hexdigest() for n in artifacts},
                    source_manifest=ab.read_json(ab.DEFAULT_OUTPUT / "manifest.json"),
                    heldout_source_sha256=hashlib.sha256(HELDOUT_SOURCE.read_bytes()).hexdigest(),
                    stage1_conditions=len(conditions), stage1_requests=len(conditions) * labels.REPEATS,
                    heldout_cases=len(heldout), heldout_families=len({family(c) for c in heldout}),
                    selection_rule=labels.SELECTION_RULE, repeat_count=labels.REPEATS,
                    cache_policy="Fresh DB, unique run_id per fixed repeat/transport attempt; identical wire prompt across repeats.",
                    inference="Original temperature=0, no thinking, no new inference config")
    ab.write_json(output / "manifest.json", manifest, exclusive=True)
    print(json.dumps({k: manifest[k] for k in ("stage1_conditions", "stage1_requests", "heldout_cases", "heldout_families")}), flush=True)


def latest(history):
    out = {}
    for row in history:
        key = row["stage"], row["condition_id"], row["version"], row["repeat"]
        if key in out and out[key].get("raw_output") is not None:
            raise ValueError("SEMANTIC_RESAMPLING_FORBIDDEN")
        out[key] = row
    return out


def production_score(case, condition, raw):
    """Resolve experiment aliases first, then use the UNCHANGED real parser."""
    try:
        if raw.strip() == "NOOP":
            canonical = raw
        else:
            f = fields(raw.strip())
            if len(raw.strip().splitlines()) != 1 or len(f) != 3 or f[0] != "BOUND":
                raise ValueError("EXPERIMENT_REQUIRES_BOUND_THREE_FIELDS_OR_NOOP")
            support = refs(f[2])
            mapping = condition["short_to_durable"]
            if any(s not in mapping for s in support):
                raise ValueError("UNKNOWN_EXPERIMENT_ALIAS")
            if len(support) != len(set(support)):
                raise ValueError("DUPLICATE_EXPERIMENT_ALIAS")
            canonical = "BOUND | " + escaped(f[1]) + " | " + (",".join(mapping[s] for s in support) or "NONE")
        scored = ab.score(case, canonical)
        if scored["parsed"] and case["expected"].get("accepted_values"):
            # Identity equivalents were frozen BEFORE calls, never ad-hoc leniency.
            scored["value_correct"] = any(ab.normalize(scored["parsed"]["value"]) == ab.normalize(v)
                                          for v in case["expected"]["accepted_values"])
            scored["passed"] = all(scored[k] for k in ("protocol_valid", "runtime_valid", "decision_correct", "value_correct", "support_correct"))
            scored["false_binding"] = scored["parsed"]["state"] == "BOUND" and not all(scored[k] for k in ("decision_correct", "value_correct", "support_correct"))
        scored["canonical_output"] = canonical
        return scored
    except (ValueError, KeyError, TypeError) as exc:
        return dict(parsed=None, protocol_valid=False, runtime_valid=False, passed=False,
                    false_binding=False, error=str(exc))


def diagnostic_score(case, condition, raw):
    """Validate a separate candidate report. No parser/apply/transaction call."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_DIAGNOSTIC_KEY")
            result[key] = value
        return result
    out = dict(protocol_valid=False, runtime_committed=False, passed=False, error=None)
    try:
        data = json.loads(raw, object_pairs_hook=unique)
        assert set(data) == {"answer_candidates", "excluded_facts", "blocking_fact_ids", "decision"}
        out["parsed"] = data
        assert data["decision"] in {"BOUND", "NOOP"}
        mapping = condition["short_to_durable"]
        support_ids, excluded_ids = set(), set()
        mapped_candidates = []
        for candidate in data["answer_candidates"]:
            assert set(candidate) == {"value", "support_fact_ids"}
            ids = candidate["support_fact_ids"]
            assert isinstance(ids, list) and len(ids) == len(set(ids)) and set(ids) <= set(mapping)
            support_ids.update(ids)
            mapped_candidates.append(dict(value=candidate["value"], support_fact_ids=[mapping[i] for i in ids]))
        for excluded in data["excluded_facts"]:
            assert set(excluded) == {"fact_id", "reason_code"}
            assert excluded["fact_id"] in mapping and excluded["fact_id"] not in excluded_ids
            assert excluded["reason_code"] in {"ENTITY_MISMATCH", "RELATION_MISMATCH", "SCOPE_MISMATCH", "ROLE_INSUFFICIENT", "BACKGROUND"}
            excluded_ids.add(excluded["fact_id"])
        blockers = data["blocking_fact_ids"]
        assert isinstance(blockers, list) and len(blockers) == len(set(blockers)) and set(blockers) <= set(mapping)
        assert not excluded_ids & (support_ids | set(blockers))
        uncovered = set(mapping) - (support_ids | excluded_ids | set(blockers))
        if uncovered:
            raise ValueError("UNREPORTED_FACT_IDS:" + ",".join(sorted(uncovered)))
        expected = case["expected"]
        accepted = expected.get("accepted_values", [expected["value"]])
        correct = [c for c in mapped_candidates if any(ab.normalize(c["value"]) == ab.normalize(v) for v in accepted)
                   and set(c["support_fact_ids"]) == set(expected["support_fact_ids"])]
        if expected["decision"] == "BOUND":
            if not correct:
                category = "candidate_missing_or_wrong"
            elif len(mapped_candidates) > 1:
                category = "irrelevant_or_duplicate_candidates"
            elif blockers:
                category = "spurious_blockers"
            elif data["decision"] == "NOOP":
                category = "candidate_correct_decision_noop"
            else:
                category = "candidate_and_decision_correct"
        else:
            category = "negative_correct" if data["decision"] == "NOOP" else "negative_false_bound"
        out.update(protocol_valid=True, parsed=data, durable_candidates=mapped_candidates, category=category,
                   passed=category in {"candidate_and_decision_correct", "negative_correct"})
    except (ValueError, TypeError, KeyError, AssertionError) as exc:
        out["error"] = str(exc) or "INVALID_DIAGNOSTIC_SCHEMA"
    return out


def result_rows(output):
    path = output / "results.jsonl"
    return ab.rows(path) if path.exists() else []


def require_complete(output, stage):
    filename = "diagnostic_conditions.jsonl" if stage == "diagnostic" else f"stage{stage}_conditions.jsonl"
    expected = {(stage, c["condition_id"], c["version"], repeat)
                for c in ab.rows(output / filename) for repeat in range(1, labels.REPEATS + 1)}
    actual = latest(result_rows(output))
    missing = [key for key in expected if key not in actual or actual[key].get("raw_output") is None]
    assert not missing, f"Incomplete stage {stage}: {len(missing)} unanswered fixed repeats"


def aggregate(rows):
    stats = ab.metrics(rows)
    proposals = [r for r in rows if (r.get("raw_output") or "").strip().startswith("BOUND")]
    stats["bound_proposals"] = len(proposals)
    stats["rejected_bound_proposals"] = sum(not r.get("score", {}).get("runtime_valid") for r in proposals)
    stats["unsafe_bound_proposals"] = sum(not r.get("score", {}).get("runtime_valid")
                                          or r.get("score", {}).get("false_binding", False) for r in proposals)
    groups = defaultdict(list)
    for r in rows:
        groups[r["family"]].append(r)
    stats["families"] = len(groups)
    stats["family_macro_accuracy"] = sum(sum(r.get("score", {}).get("passed", False) for r in g) / len(g)
                                         for g in groups.values()) / len(groups) if groups else None
    by_condition = defaultdict(list)
    for r in rows:
        by_condition[r["condition_id"]].append(r)
    stats["repeat_stable_conditions"] = sum(len(g) == labels.REPEATS and len({digest((r.get("score") or {}).get("parsed")) for r in g}) == 1
                                            and all(r.get("raw_output") is not None for r in g) for g in by_condition.values())
    stats["all_repeats_correct_conditions"] = sum(len(g) == labels.REPEATS and all(r.get("score", {}).get("passed") for r in g) for g in by_condition.values())
    stats["conditions"] = len(by_condition)
    return stats


def diagnose(output):
    assert_frozen(output)
    require_complete(output, "1")
    results = list(latest(result_rows(output)).values())
    core = [r for r in results if r["stage"] == "1" and r["version"] in {"P17-full", "P17-local"} and r["group"] == "core"]
    assert core and all(r.get("raw_output") is not None for r in core), "Complete short/local stage first"
    cases = {c["case_id"]: c for c in ab.rows(output / "stage1_cases.jsonl")}
    targets = {r["case_id"] for r in core if not r["score"]["passed"] and r["expected"]["decision"] == "BOUND"}
    # Fixed controls reveal whether diagnostics simply BOUND everything.
    targets |= {"dev_4961.Q1.1", "dev_10616.Q1.1", "dev_4784.Q1.1"}
    conditions = [make_condition(cases[cid], "diagnostic." + cid, "diagnostic", "diagnostic", group="diagnostic") for cid in sorted(targets)]
    save_lines(output / "diagnostic_conditions.jsonl", conditions)
    ab.write_json(output / "diagnostic_manifest.json", dict(
        trigger="Either short variant missed/misbound a positive case, plus three fixed NOOP controls", selected_cases=sorted(targets),
        conditions_sha256=hashlib.sha256((output / "diagnostic_conditions.jsonl").read_bytes()).hexdigest(),
        no_runtime_commit=True), exclusive=True)
    print(f"Frozen {len(conditions)} independent diagnostic conditions", flush=True)


def select(output):
    assert_frozen(output)
    require_complete(output, "1")
    if (output / "diagnostic_conditions.jsonl").exists():
        require_complete(output, "diagnostic")
    results = list(latest(result_rows(output)).values())
    core = [r for r in results if r["stage"] == "1" and r["group"] == "core"]
    groups = {v: aggregate([r for r in core if r["version"] == v]) for v in ("B", "P17-full", "P17-local")}
    assert len({m["n"] for m in groups.values()}) == 1
    assert all(not m["api_errors"] for m in groups.values())
    candidates = [v for v in ("P17-full", "P17-local") if groups[v]["false_bindings"] == 0]
    # If both have false binding regressions, freeze the better diagnostic arm
    # for regression only; never silently approve it for production.
    safe = bool(candidates)
    candidates = candidates or ["P17-full", "P17-local"]
    selected = max(candidates, key=lambda v: (groups[v]["family_macro_accuracy"], v == "P17-local"))
    selection = dict(selected=selected, stage1_metrics=groups, safety_gate_passed=safe,
                     stage1_net_gain=groups[selected]["family_macro_accuracy"] > groups["B"]["family_macro_accuracy"],
                     rule=labels.SELECTION_RULE, production_default="B", production_changed=False,
                     selected_instruction_sha256=ab.sha(prompts.MEMORY_BIND_P17))
    conditions = []
    for name, group in (("regression_cases.jsonl", "regression55"), ("heldout_cases.jsonl", "heldout")):
        for case in ab.rows(output / name):
            for version in ("B", selected):
                conditions.append(make_condition(case, group + "." + case["case_id"], version, "2", group=group))
    save_lines(output / "stage2_conditions.jsonl", conditions)
    selection["conditions_sha256"] = hashlib.sha256((output / "stage2_conditions.jsonl").read_bytes()).hexdigest()
    ab.write_json(output / "selection.json", selection, exclusive=True)
    print(json.dumps(selection, ensure_ascii=False, indent=2), flush=True)


async def run(output, stage, env_file, concurrency=3, retry_api_errors=False):
    manifest = assert_frozen(output)
    filename = "diagnostic_conditions.jsonl" if stage == "diagnostic" else f"stage{stage}_conditions.jsonl"
    if stage in {"2", "diagnostic"}:
        control = ab.read_json(output / ("selection.json" if stage == "2" else "diagnostic_manifest.json"))
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == control["conditions_sha256"]
    conditions = ab.rows(output / filename)
    case_files = ("regression_cases.jsonl", "heldout_cases.jsonl") if stage == "2" else ("stage1_cases.jsonl",)
    cases = {c["case_id"]: c for name in case_files for c in ab.rows(output / name)}
    ab._load_env_file(env_file)
    source = manifest["source_manifest"]
    cfg_data = ab.read_json(Path(source["source_run"]) / "config.resolved.json")
    params = source["source_api_parameters"]
    cfg_data["api"].update(model_seed=params.get("seed"), max_output_tokens=params["max_tokens"])
    cfg = ab._api_config(cfg_data)
    store = ab.SQLiteEventStore(str(output / "api_calls.sqlite"))
    client = ab.OpenAICompatibleClient(cfg, store=store)
    assert {k: v for k, v in client._request_payload("MEMORY", []).items() if k != "messages"} == params
    identity = {k: v for k, v in asdict(cfg).items() if k != "api_key"}
    if (output / "api_identity.json").exists():
        assert ab.read_json(output / "api_identity.json") == identity
    else:
        ab.write_json(output / "api_identity.json", identity, exclusive=True)
    history = result_rows(output)
    existing = latest(history)
    sem = asyncio.Semaphore(concurrency)
    tasks = [(c, repeat) for repeat in range(1, labels.REPEATS + 1)
             for c in (conditions if repeat % 2 else list(reversed(conditions)))]

    async def one(condition, repeat):
        key = stage, condition["condition_id"], condition["version"], repeat
        previous = existing.get(key)
        if previous and (previous.get("raw_output") is not None or not retry_api_errors):
            return
        async with sem:
            case = cases[condition["case_id"]]
            assert digest(condition["messages"]) == condition["message_sha256"]
            attempt = 1 + sum((r["stage"], r["condition_id"], r["version"], r["repeat"]) == key for r in history)
            # No nonce in wire data. Unique local run_id bypasses response cache.
            run_id = "memory-isolation." + digest(key)[:24] + f".attempt{attempt}"
            assert not store.list_model_calls(run_id), "Never reuse a cached experimental call"
            result = {k: condition[k] for k in ("stage", "condition_id", "case_id", "family", "group", "version", "expected", "message_sha256")}
            result.update(repeat=repeat, evaluation_attempt=attempt, raw_output=None, api_error=None)
            try:
                raw = await client.complete(run_id=run_id, interface="MEMORY", messages=condition["messages"], agent_role="HIGH")
                result["raw_output"] = raw
            except Exception as exc:
                result["api_error"] = str(exc)
            else:
                result["score"] = (diagnostic_score if stage == "diagnostic" else production_score)(case, condition, raw)
            calls = store.list_model_calls(run_id)
            assert all(not c.cache_hit for c in calls)
            result.update(call_ids=[c.call_id for c in calls], cache_hits=0,
                          input_tokens=sum(c.input_tokens or 0 for c in calls), output_tokens=sum(c.output_tokens or 0 for c in calls))
            with (output / "results.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            shown = result["raw_output"] if result["raw_output"] is not None else result["api_error"]
            print(f"{stage}/{condition['version']}/r{repeat} {condition['condition_id']}: "
                  f"{str(shown).replace(chr(10), ' ')[:180]} pass={result.get('score', {}).get('passed', False)}", flush=True)
    try:
        await asyncio.gather(*(one(c, repeat) for c, repeat in tasks))
    finally:
        store.connection.close()
    report(output)


def report(output):
    results = list(latest(result_rows(output)).values())
    groups = defaultdict(list)
    for r in results:
        groups[f"{r['stage']}.{r['group']}.{r['version']}"].append(r)
    summary = {key: aggregate(rows) for key, rows in sorted(groups.items()) if not key.startswith("diagnostic")}
    # A/A-neutral run on six selected cases, so compare B on exactly those
    # cases, never against B's larger and differently weighted core suite.
    neutral_ids = {r["condition_id"] for r in results if r["stage"] == "1" and r["version"] == "A-neutral"}
    matched_neutral = {v: aggregate([r for r in results if r["stage"] == "1" and r["version"] == v
                                    and r["condition_id"] in neutral_ids]) for v in ("A", "A-neutral", "B")}
    diagnostics = Counter(r.get("score", {}).get("category", "error") for r in results if r["stage"] == "diagnostic")
    telemetry = dict(completed=len(results), valid_responses=sum(r.get("raw_output") is not None for r in results),
                     unresolved_api_errors=sum(bool(r["api_error"]) for r in results),
                     transport_only_retry_evaluations=len(result_rows(output)) - len(results))
    dbpath = output / "api_calls.sqlite"
    if dbpath.exists():
        db = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
        calls = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM model_calls")]
        db.close()
        telemetry.update(http_attempts=len(calls), cache_hits=sum(bool(c.get("cache_hit")) for c in calls),
                         failed_attempts=sum(bool(c.get("error")) for c in calls),
                         input_tokens=sum(c.get("input_tokens") or 0 for c in calls),
                         output_tokens=sum(c.get("output_tokens") or 0 for c in calls),
                         errors=dict(Counter(c["error"] for c in calls if c.get("error"))))
    stability = {}
    for version in sorted({r["version"] for r in results if r["stage"] == "2"}):
        lookup = {(r["case_id"], r["repeat"]): r for r in results if r["stage"] == "2" and r["version"] == version}
        pairs = []
        for c in ab.rows(output / "heldout_cases.jsonl"):
            if not c.get("stability_parent"):
                continue
            for repeat in range(1, labels.REPEATS + 1):
                a, b = lookup.get((c["stability_parent"], repeat)), lookup.get((c["case_id"], repeat))
                if not a or not b:
                    continue
                def signature(r):
                    p = r.get("score", {}).get("parsed") or {}
                    return p.get("state"), ab.normalize(p.get("value")), sorted(p.get("support_fact_ids", []))
                valid = all(r.get("raw_output") is not None and r.get("score", {}).get("protocol_valid") for r in (a, b))
                pairs.append((valid and signature(a) == signature(b), all(r.get("score", {}).get("passed") for r in (a, b))))
        stability[version] = dict(pairs=len(pairs), same_answer_and_durable_support=sum(x for x, _ in pairs), both_correct=sum(y for _, y in pairs))
    ab.write_json(output / "summary.json", dict(groups=summary, matched_neutral=matched_neutral,
                                               diagnostics=dict(diagnostics), telemetry=telemetry, stability=stability))
    lines = ["# MEMORY 输入隔离实验", "", "生产默认仍为 B；UPDATE、PLAN、REBIND、绑定事务均未修改。每条件固定 3 次；无语义重试；诊断不入库。", "",
             "| 分组 | 完全正确 | 正确绑定/应绑定 | 正确NOOP/应NOOP | 误绑 | 题目家族宏平均 |", "|---|---:|---:|---:|---:|---:|"]
    for k, m in summary.items():
        lines.append(f"| {k} | {m['passed']}/{m['n']} | {m['correct_bindings']}/{m['bindable']} | {m['correct_noops']}/{m['should_noop']} | {m['false_bindings']} | {m['family_macro_accuracy']:.3f} |")
    lines += ["", "诊断分类：" + json.dumps(dict(diagnostics), ensure_ascii=False), "", "API：" + json.dumps(telemetry, ensure_ascii=False),
              "", "顺序/编号/噪声不变性（支持映射回持久 ID）：" + json.dumps(stability, ensure_ascii=False),
              "", "## 逐条件重复结果", "", "| 阶段 | 条件 | 版本 | 重复 | 正确 | 输出 |", "|---|---|---|---:|---|---|"]
    for r in sorted(results, key=lambda r: (r["stage"], r["condition_id"], r["version"], r["repeat"])):
        raw = (r.get("raw_output") or r.get("api_error") or "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['stage']} | {r['condition_id']} | {r['version']} | {r['repeat']} | {bool(r.get('score', {}).get('passed'))} | {raw} |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(dict(groups=summary, diagnostics=dict(diagnostics), telemetry=telemetry), ensure_ascii=False, indent=2), flush=True)


def audit(output):
    """Recompute judgments without API calls; retain all original result rows."""
    manifest = assert_frozen(output)
    for stage in ("1", "diagnostic", "2"):
        require_complete(output, stage)
    stage1 = {c["case_id"]: c for c in ab.rows(output / "stage1_cases.jsonl")}
    stage2 = {c["case_id"]: c for f in ("regression_cases.jsonl", "heldout_cases.jsonl") for c in ab.rows(output / f)}
    conditions = {(c["stage"], c["condition_id"], c["version"]): c
                  for f in ("stage1_conditions.jsonl", "diagnostic_conditions.jsonl", "stage2_conditions.jsonl")
                  for c in ab.rows(output / f)}
    db = sqlite3.connect(f"file:{output / 'api_calls.sqlite'}?mode=ro", uri=True)
    calls = {c["call_id"]: c for row in db.execute("SELECT payload_json FROM model_calls") for c in [json.loads(row[0])]}
    db.close()
    diagnostic_details, result_count = [], 0
    for r in latest(result_rows(output)).values():
        cond = conditions[r["stage"], r["condition_id"], r["version"]]
        assert r["message_sha256"] == digest(cond["messages"])
        for cid in r["call_ids"]:
            call = calls[cid]
            assert not call.get("cache_hit")
            assert call["raw_request"]["messages"] == cond["messages"]
            assert {k: v for k, v in call["raw_request"].items() if k != "messages"} == manifest["source_manifest"]["source_api_parameters"]
        case = (stage2 if r["stage"] == "2" else stage1)[r["case_id"]]
        score = (diagnostic_score if r["stage"] == "diagnostic" else production_score)(case, cond, r["raw_output"])
        for key in ("passed", "protocol_valid"):
            assert score[key] == r["score"][key], (r["condition_id"], key)
        if r["stage"] == "diagnostic":
            diagnostic_details.append(dict(case_id=r["case_id"], repeat=r["repeat"], score=score))
        else:
            assert score["runtime_valid"] == r["score"]["runtime_valid"]
            assert score["false_binding"] == r["score"]["false_binding"]
        result_count += 1
    # More specific diagnostic error details are an analysis artifact, not
    # replacement responses or changed success/failure labels.
    ab.write_json(output / "diagnostic_audit.json", diagnostic_details)
    result = dict(checked_results=result_count, frozen_contracts_unchanged=True,
                  all_fixed_repeats_present=True, wire_parameters_identical=True,
                  same_prompt_each_repeat=True, local_cache_hits=0, score_recomputation_agrees=True,
                  diagnostic_runtime_commits=0,
                  implementation_sha256={name: ab.sha((ROOT / name).read_text()) for name in (
                      "scripts/evaluate_r2_memory_isolation.py", "scripts/evaluate_r2_memory_chain.py",
                      "delaybind_core/memory_experiments_r2.py", "tests/test_r2_memory_isolation.py")})
    ab.write_json(output / "final_audit.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "diagnose", "select", "report", "audit"])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage", choices=["1", "2", "diagnostic"], default="1")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument("--concurrency", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--retry-api-errors", action="store_true")
    args = parser.parse_args()
    if args.action == "run":
        asyncio.run(run(args.output, args.stage, args.env_file, args.concurrency, args.retry_api_errors))
    else:
        {"prepare": prepare, "diagnose": diagnose, "select": select, "report": report, "audit": audit}[args.action](args.output)


if __name__ == "__main__":
    main()
