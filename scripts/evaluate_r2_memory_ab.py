"""Freeze and evaluate MEMORY in isolation; never call PLAN/UPDATE/ANSWER.

prepare: read the historical database read-only, label 14 exact snapshots, then
create controlled perturbations before observing any new model outputs.
run: paired live A/B requests with identical data and API parameters.
Every response is also applied to a disposable runtime snapshot. No decision
is carried to the next case, and neither arm receives semantic retry hints.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib.util
import inspect
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delaybind_core import prompts_r2 as prompts
from delaybind_core.api import OpenAICompatibleClient
from delaybind_core.archive import SentenceArchive
from delaybind_core.cli import _api_config, _load_env_file
from delaybind_core.context_r2 import build_memory_context, persist_memory_context
from delaybind_core.memory_r2 import apply_memory
from delaybind_core.navigation_r2 import query_projection, query_ready, refresh, assert_invariants
from delaybind_core.protocol_r2 import fact_alias_map, parse_memory
from delaybind_core.review_jobs_r2 import ensure_use, evidence_signature
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import StateR2, MemoryContextR2, fact_id
from delaybind_core.schema_v52 import FactNode, digest
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.text_views_v52 import escaped

DEFAULT_SOURCE = ROOT / "results/v52-r2-2wiki50-seed4-smoke8-text7-fact-only-run7-high-recall-update"
DEFAULT_OUTPUT = ROOT / "results/r2-memory-bind-ab-text15-text16"
FIXTURES = ROOT / "tests/fixtures/r2_memory_ab"
spec = importlib.util.spec_from_file_location("memory_ab_annotations", FIXTURES / "annotations.py")
annotations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(annotations)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def latest_results(history):
    """Transport-only retries supersede failures; never discard a model answer."""
    latest = {}
    for result in history:
        key = result["case_id"], result["version"]
        previous = latest.get(key)
        if previous is not None and previous.get("raw_output") is not None:
            raise ValueError("SEMANTIC_RESAMPLING_FORBIDDEN:" + repr(key))
        latest[key] = result
    return list(latest.values())


def write_json(path, value, *, exclusive=False):
    with Path(path).open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def assert_frozen_contracts(*, check_runtime=True):
    frozen = read_json(FIXTURES / "frozen_contracts.txt")
    assert prompts.PROMPT_VERSION == frozen["prompt_version"]
    for name, expected in frozen["constant_sha256"].items():
        assert sha(getattr(prompts, name)) == expected, f"Frozen prompt changed: {name}"
    for name, expected in frozen["renderer_sha256"].items():
        assert sha(inspect.getsource(getattr(prompts, name))) == expected, f"Frozen renderer changed: {name}"
    if check_runtime:
        for filename, expected in frozen["file_sha256"].items():
            assert sha((ROOT / filename).read_text()) == expected, f"Frozen code changed: {filename}"
    return frozen


def original_messages(payload):
    """A is exactly text-15, including its original system message."""
    return [dict(role="system", content=prompts.SYSTEM_FACT_ONLY), dict(role="user", content=(
        "Protocol: " + prompts.PROMPT_VERSION + "\n" + prompts.MEMORY_FACT_ONLY
        + "\nInput data:\n" + prompts.request_view("MEMORY", payload)))]


def make_payload(state, ctx, question):
    return {**ctx.model_dump(mode="json"), "question": question,
            "query_instance": next(q for q in query_projection(state)["queries"] if q["id"] == ctx.query_id),
            "old_binding": state.binding_store[ctx.expected_binding_id].model_dump() if ctx.expected_binding_id else None,
            "staged_reviews": []}


def make_case(*, case_id, state, ctx, question, expected, old_output=None,
              source_messages=None, order=None, stage=1, parent=None, kind="exact", run_id=None):
    payload = make_payload(state, ctx, question)
    mapping = fact_alias_map(ctx.allowed_fact_ids)
    texts = {n["fact_id"]: n["text"] for n in ctx.working_memory.navigation["facts"]}
    candidates = [dict(short_id=alias, durable_id=fid, text=texts[fid]) for alias, fid in mapping.items()]
    if order:
        by_text = {c["text"]: c for c in candidates}
        assert len(by_text) == len(candidates) and set(order) == set(by_text)
        candidates = [by_text[text] for text in order]
    data = prompts.request_view("MEMORY", payload)
    start = data.index("Eligible facts:\n") + len("Eligible facts:\n")
    end = data.index("\n\nEffective upstream bindings:", start)
    data = data[:start] + ("\n".join(f"{c['short_id']} | {escaped(c['text'])}" for c in candidates) or "NONE") + data[end:]
    a = original_messages(payload)
    b = prompts.messages("MEMORY", payload)
    for messages in (a, b):
        prefix, _ = messages[1]["content"].split("Input data:\n", 1)
        messages[1]["content"] = prefix + "Input data:\n" + data
    if source_messages is not None:
        assert a == source_messages, f"Not an exact replay: {case_id}"
    assert a[1]["content"].split("Input data:\n", 1)[1] == b[1]["content"].split("Input data:\n", 1)[1]
    assert query_ready(state, ctx.query_id)
    assert not re.search(r"\?[A-Za-z_][A-Za-z_0-9]*", payload["query_instance"]["rendered_query"])
    assert ctx.allowed_mode == "BIND" and ctx.fact_only and ctx.phase == "FINAL"
    return dict(case_id=case_id, parent_case_id=parent, stage=stage, perturbation=kind,
                sample_id=case_id.split(".")[0], source_run_id=run_id,
                mode=ctx.allowed_mode, current_query=payload["query_instance"]["rendered_query"],
                cardinality=ctx.expected_cardinality,
                effective_upstream_bindings=payload["query_instance"]["bound_inputs"],
                existing_binding=payload["old_binding"], eligible_facts=candidates,
                short_to_durable=mapping, old_output=old_output, expected=expected,
                context=ctx.model_dump(mode="json"), state=state.model_dump(mode="json"),
                payload=payload, messages={"A": a, "B": b}, input_sha256=sha(data))


def perturb(base, kind, texts, support_texts, *, decision="BOUND", query_text=None):
    state = StateR2.model_validate(copy.deepcopy(base["state"]))
    original = MemoryContextR2.model_validate(base["context"])
    qid = original.query_id
    ex = state.executions[qid]
    assert not ex.current_binding_id
    # Each perturbation has a fresh, isolated query snapshot. It is not fed back
    # into the original trajectory and does not fabricate downstream requests.
    state.uses = {k: u for k, u in state.uses.items() if u.query_id != qid}
    state.route_index[qid] = []
    if query_text:
        state.plan.queries = [q.model_copy(update={"template": query_text}) if q.id == qid else q for q in state.plan.queries]
    for text in texts:
        fid = fact_id(text, [])
        state.facts[fid] = FactNode(fact_id=fid, text=text, source_refs=(), observed_window=state.read_watermark)
        state.route_index[qid].append(fid)
        ensure_use(state, qid, fid, status="PENDING")
    state.route_index[qid].sort()
    ex.evidence_revision += 1
    review = state.reviews[original.review_id]
    review.inbox_revision = ex.evidence_revision
    review.candidate_bucket_version = digest(state.route_index[qid])
    review.evidence_signature = evidence_signature(state, qid)
    review.required_use_ids = sorted(u.use_id for u in state.uses.values() if u.query_id == qid)
    refresh(state)
    assert_invariants(state)
    # Context construction is read-only and needs only state/config/run_id.
    from types import SimpleNamespace
    runtime = SimpleNamespace(state=state, config=RunnerConfig(**state.run_metadata["config"]), run_id=base["source_run_id"])
    ctx = build_memory_context(runtime, review.review_id)
    expected = dict(decision=decision, value=base["expected"]["value"] if decision == "BOUND" else None,
                    support_fact_ids=[fact_id(t, []) for t in support_texts],
                    reason="受控扰动：" + kind, annotation_basis="visible_facts_only")
    if kind in {"parent_inverse", "father_with_role_evidence"}:
        expected["value"] = "Jobst of Limburg"
    return make_case(case_id=base["case_id"] + "." + kind, state=state, ctx=ctx,
                     question=base["payload"]["question"], expected=expected, order=texts, stage=2,
                     parent=base["case_id"], kind=kind, run_id=base["source_run_id"])


def priority(case):
    rank = {"dev_7920.Q1.2": 0, "dev_4961.Q1.2": 1, "dev_10616.Q1.1": 2}
    return rank.get(case.get("parent_case_id") or case["case_id"], 3), case["case_id"]


def prepare(source, output):
    frozen = assert_frozen_contracts()
    source = source.resolve()
    db_path = source / "experiment.sqlite"
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    counts, cases, api_parameters = Counter(), [], []
    calls = db.execute("SELECT rowid,payload_json FROM model_calls WHERE interface='MEMORY' ORDER BY rowid").fetchall()
    for ordinal, encoded in calls:
        call = json.loads(encoded)
        assert not call.get("error") and not call.get("cache_hit")
        context = json.loads(db.execute("SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
                                       (call["run_id"], call["context_id"])).fetchone()[0])
        context.pop("interface")
        ctx = MemoryContextR2.model_validate(context)
        state = StateR2.model_validate_json(db.execute("SELECT state_json FROM memory_transactions WHERE run_id=? AND revision=?",
                                                     (call["run_id"], ctx.state_revision)).fetchone()[0])
        source_messages = call["raw_request"]["messages"]
        question = source_messages[1]["content"].split("Question:\n", 1)[1].split("\n\nEvidence mode:", 1)[0]
        sample_id = re.search(r"dev_\d+", call["run_id"])[0]
        key = f"{sample_id}.{ctx.query_id}"
        counts[key] += 1
        case_id = f"{key}.{counts[key]}"
        expected = copy.deepcopy(annotations.LABELS[case_id])
        mapping = fact_alias_map(ctx.allowed_fact_ids)
        expected["support_fact_ids"] = [mapping[a] for a in expected.pop("support_aliases")]
        expected["annotation_basis"] = "visible_facts_only"
        case = make_case(case_id=case_id, state=state, ctx=ctx, question=question, expected=expected,
                         source_messages=source_messages, old_output=call["parsed_output"], run_id=call["run_id"])
        case["source_call_id"], case["source_call_ordinal"] = call["call_id"], ordinal
        cases.append(case)
        api_parameters.append({k: v for k, v in call["raw_request"].items() if k != "messages"})
    db.close()
    assert len(cases) == 14 and {c["case_id"] for c in cases} == set(annotations.LABELS)
    assert all(p == api_parameters[0] for p in api_parameters)
    for base in list(cases):
        original_texts = [f["text"] for f in base["eligible_facts"]]
        support = set(base["expected"]["support_fact_ids"])
        support_texts = [f["text"] for f in base["eligible_facts"] if f["durable_id"] in support]
        if base["expected"]["decision"] != "BOUND":
            continue
        if len(original_texts) > 1:
            cases.append(perturb(base, "reverse_order", list(reversed(original_texts)), support_texts))
        wrong_entity, wrong_relation, paraphrase, contradiction = annotations.PERTURBATIONS[base["case_id"]]
        for kind, noise in [("wrong_entity", wrong_entity), ("wrong_relation", wrong_relation)]:
            cases.append(perturb(base, kind, [noise, *original_texts], support_texts))
        assert len(support_texts) == 1
        changed = [paraphrase if t == support_texts[0] else t for t in original_texts]
        cases.append(perturb(base, "equivalent_expression", changed, [paraphrase]))
        cases.append(perturb(base, "counterevidence", [*original_texts, contradiction], [], decision="NOOP"))
    role = next(c for c in cases if c["case_id"] == "dev_10616.Q1.1")
    text = role["eligible_facts"][0]["text"]
    cases.append(perturb(role, "parent_inverse", [text], [text], query_text="Who is Georg Ernst Of Limburg Stirum's parent?"))
    male = "Jobst of Limburg is a man."
    cases.append(perturb(role, "father_with_role_evidence", [text, male], [text, male]))
    female = "Jobst of Limburg is a woman."
    cases.append(perturb(role, "female_parent_guard", [text, female], [], decision="NOOP"))
    cases.sort(key=lambda c: (c["stage"], *priority(c)))
    output.mkdir(parents=True, exist_ok=True)
    suite = output / "cases.jsonl"
    with suite.open("x", encoding="utf-8") as stream:
        for case in cases:
            stream.write(json.dumps(case, ensure_ascii=False) + "\n")
    write_json(output / "manifest.json", dict(
        experiment="MEMORY-only A/B; no PLAN, UPDATE, RECALL, ANSWER or final EM",
        source_run=str(source), source_db_sha256=hashlib.sha256(db_path.read_bytes()).hexdigest(),
        source_api_parameters=api_parameters[0], suite_sha256=hashlib.sha256(suite.read_bytes()).hexdigest(),
        stage_counts=dict(Counter(str(c["stage"]) for c in cases)),
        bound_counts=dict(Counter(str(c["stage"]) for c in cases if c["expected"]["decision"] == "BOUND")),
        annotations_sha256=sha((FIXTURES / "annotations.py").read_text()), frozen_contracts=frozen,
        baseline_prompt_version=prompts.PROMPT_VERSION, bind_prompt_version=prompts.MEMORY_BIND_PROMPT_VERSION,
        plan_cardinality_unchanged=True, rebind_unchanged=True,
        labels="Fixed using only each visible snapshot, before new A/B calls. No gold answers supplied.",
        perturbation_note="Reverse-order applied only to 2 snapshots with >1 fact; 9 bindable snapshots each get entity noise, relation noise, equivalent wording and counterevidence; 3 separate role controls.",
    ), exclusive=True)
    print(f"Frozen {len(cases)} cases: {dict(Counter(c['stage'] for c in cases))}", flush=True)


def disposable_runtime(case):
    store = SQLiteEventStore()
    state = StateR2.model_validate(case["state"])
    run_id = case["source_run_id"]
    revision = state.state_revision
    # Seed exactly the saved pre-call revision into an in-memory DB only.
    with store.connection:
        store.connection.execute("INSERT INTO memory_transactions VALUES (?,?,?,?,?,?,?)", (
            run_id, "OFFLINE-SEED", None, revision - 1, revision, state.model_dump_json(), "null"))
    runtime = RuntimeR2(run_id=run_id, plan=state.plan, archive=SentenceArchive(store, run_id),
                        store=store, config=RunnerConfig(**state.run_metadata["config"]))
    ctx = MemoryContextR2.model_validate(case["context"])
    persist_memory_context(runtime, ctx)
    return runtime, ctx


def normalize(value):
    if isinstance(value, str):
        return re.sub(r"^the\s+", "", " ".join(value.casefold().split())).rstrip(".")
    if isinstance(value, list):
        return sorted(digest(normalize(v)) for v in value)
    return value


def score(case, raw):
    out = dict(parsed=None, protocol_valid=False, runtime_valid=False, decision_correct=False,
               value_correct=False, support_correct=False, passed=False, error=None)
    runtime, ctx = disposable_runtime(case)
    try:
        response = parse_memory(raw, ctx)
        result = response.operations[0].binding_result
        out["parsed"] = result.model_dump(mode="json")
        out["protocol_valid"] = bool(raw.strip() == "NOOP" or raw.strip().startswith("BOUND |"))
        expected = case["expected"]
        out["decision_correct"] = result.state == expected["decision"]
        out["value_correct"] = normalize(result.value) == normalize(expected["value"])
        out["support_correct"] = set(result.support_fact_ids) == set(expected["support_fact_ids"])
        apply_memory(runtime, response, ctx)
        out["runtime_valid"] = True
        out["passed"] = all(out[k] for k in ("protocol_valid", "runtime_valid", "decision_correct", "value_correct", "support_correct"))
    except (ValueError, KeyError, TypeError) as exc:
        out["error"] = str(exc)
    finally:
        runtime.store.connection.close()
    out["false_binding"] = bool(out["parsed"] and out["parsed"]["state"] == "BOUND" and
                                (case["expected"]["decision"] != "BOUND" or not out["value_correct"] or not out["support_correct"]))
    return out


def metrics(items):
    positive = [r for r in items if r["expected"]["decision"] == "BOUND"]
    negative = [r for r in items if r["expected"]["decision"] == "NOOP"]
    emitted = [r for r in items if (r.get("score", {}).get("parsed") or {}).get("state") == "BOUND"]
    passed = lambda r: r.get("score", {}).get("passed", False)
    return dict(n=len(items), passed=sum(passed(r) for r in items),
                bindable=len(positive), correct_bindings=sum(passed(r) for r in positive),
                binding_recall=sum(passed(r) for r in positive) / len(positive) if positive else None,
                should_noop=len(negative), correct_noops=sum(passed(r) for r in negative),
                emitted_bound=len(emitted), false_bindings=sum(r.get("score", {}).get("false_binding", False) for r in items),
                false_binding_rate=sum(r.get("score", {}).get("false_binding", False) for r in items) / len(emitted) if emitted else None,
                protocol_errors=sum(bool(r.get("raw_output") is not None and not r.get("score", {}).get("protocol_valid")) for r in items),
                runtime_errors=sum(bool(r.get("raw_output") is not None and not r.get("score", {}).get("runtime_valid")) for r in items),
                api_errors=sum(bool(r.get("api_error")) for r in items))


def report(output):
    cases = rows(output / "cases.jsonl")
    history = rows(output / "results.jsonl") if (output / "results.jsonl").exists() else []
    results = latest_results(history)
    groups = defaultdict(list)
    for result in results:
        groups[f"stage{result['stage']}.{result['version']}"].append(result)
    summary = {key: metrics(items) for key, items in sorted(groups.items())}
    historical = [dict(expected=c["expected"], raw_output=c["old_output"], score=score(c, c["old_output"])) for c in cases if c["stage"] == 1]
    summary["historical_text15"] = metrics(historical)
    write_json(output / "summary.json", summary)
    telemetry = dict(expected_case_version_pairs=2 * len(cases), completed_case_version_pairs=len(results),
                     available_model_responses=sum(r.get("raw_output") is not None for r in results),
                     unresolved_api_failures=sum(bool(r.get("api_error")) for r in results),
                     historical_failed_evaluations=sum(bool(r.get("api_error")) for r in history),
                     transport_only_retry_evaluations=len(history) - len(results))
    if (output / "api_calls.sqlite").exists():
        db = sqlite3.connect(f"file:{(output / 'api_calls.sqlite').resolve()}?mode=ro", uri=True)
        calls = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM model_calls")]
        db.close()
        paid = [c for c in calls if not c.get("cache_hit")]
        telemetry.update(http_attempts=len(paid), http_failed_attempts=sum(bool(c.get("error")) for c in paid),
                         input_tokens=sum(c.get("input_tokens") or 0 for c in paid),
                         output_tokens=sum(c.get("output_tokens") or 0 for c in paid),
                         errors=dict(Counter(c["error"] for c in paid if c.get("error"))))
    write_json(output / "telemetry.json", telemetry)
    lookup = {(r["case_id"], r["version"]): r for r in results}
    perturbation_groups = defaultdict(list)
    for result in results:
        if result["stage"] == 2:
            perturbation_groups[f"{result['perturbation']}.{result['version']}"].append(result)
    details = {key: metrics(items) for key, items in sorted(perturbation_groups.items())}
    # Stability must be measured against the prelabelled answer AND support,
    # not merely agreement between two equally wrong outputs.
    stability = {}
    for version in ("A", "B"):
        invariant = [r for r in results if r["version"] == version and r["stage"] == 2
                     and r["perturbation"] in {"reverse_order", "wrong_entity", "wrong_relation", "equivalent_expression"}]
        stability[version] = dict(
            perturbations=len(invariant), correct_perturbations=sum(r.get("score", {}).get("passed", False) for r in invariant),
            base_and_perturbation_correct=sum(r.get("score", {}).get("passed", False)
                and lookup.get((r["parent_case_id"], version), {}).get("score", {}).get("passed", False) for r in invariant))
    write_json(output / "perturbation_summary.json", dict(groups=details, stability=stability))
    lines = ["# MEMORY 独立 A/B 实验", "", "A：历史 text-15 提示词；B：仅替换 Fact-only BIND 的 system 和 instruction，并加入噪声对照正例。",
             "", "PLAN、UPDATE、基数、REBIND、输出协议保持冻结。第一阶段逐字复放 14 个真实请求的数据分区；第二阶段为 41 个合成扰动。没有运行八题端到端，不能据此报告最终 EM。",
             "", "标注在 API 请求前固定，只依据当前可见事实。dev_10616 原快照缺父亲角色依据，期望 NOOP；dev_91 的工作机构按可见错误抽取事实评分，不要求凭空恢复金标。",
             "", "| 阶段/版本 | 完全正确 | 正确绑定/可绑定 | 正确 NOOP/应 NOOP | 错误绑定 | API 错误 |", "|---|---:|---:|---:|---:|---:|"]
    for key, m in summary.items():
        lines.append(f"| {key} | {m['passed']}/{m['n']} | {m['correct_bindings']}/{m['bindable']} | {m['correct_noops']}/{m['should_noop']} | {m['false_bindings']} | {m['api_errors']} |")
    lines += ["", "## 扰动分类", "", "| 扰动 | 版本 | 完全正确 |", "|---|---|---:|"]
    for key, m in details.items():
        kind, version = key.rsplit(".", 1)
        lines.append(f"| {kind} | {version} | {m['passed']}/{m['n']} |")
    lines += ["", "## 逐项结果", "", "| 测试项 | 期望 | A | B |", "|---|---|---|---|"]
    for case in cases:
        def show(version):
            r = lookup.get((case["case_id"], version))
            if r is None:
                return "待运行"
            raw = str(r.get("raw_output") or r.get("api_error")).replace("|", "\\|").replace("\n", " ")
            return ("✓ " if r.get("score", {}).get("passed") else "✗ ") + raw
        expected = case["expected"]["decision"] + (" " + str(case["expected"]["value"]) if case["expected"]["decision"] == "BOUND" else "")
        lines.append(f"| {case['case_id']} | {expected} | {show('A')} | {show('B')} |")
    lines += ["", "## API 完成度", "", json.dumps(telemetry, ensure_ascii=False),
              "", "所有调用仍使用原模型参数。只允许对没有获得模型响应的 API 失败请求补测；已有 BOUND/NOOP 不进行再次采样。失败尝试和补测记录均保留。",
              "", "## 评分与复现", "", "完全正确要求决定、结果值、支持事实集合均正确，并通过现有文本解析器和隔离快照上的真实 runtime 事务校验。错误绑定包括不应绑定时绑定、绑定错值、引用无关支持。API/协议失败不计为正确 NOOP。",
              "", "A/B 使用相同模型参数、同一数据文本、同一短 ID 映射；逐项交替 AB/BA 调用，无语义重试或强迫 BOUND。第一阶段 A 请求与历史请求逐字一致。",
              "", "每个用例单次调用，不是多次采样置信区间。二阶段扰动只评估局部稳健性，角色补证例明确属于合成输入，不视为历史原文已经有这些证据。",
              "", "cases.jsonl 保存完整快照、映射、旧输出、预标注、A/B 请求；api_calls.sqlite 保存真实 API 请求/响应；results.jsonl 保存逐项解析与 runtime 校验；manifest.json 保存冻结文件及输入哈希。"]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


async def run(output, stages, env_file, concurrency=1, retry_api_errors=False):
    assert_frozen_contracts()
    manifest = read_json(output / "manifest.json")
    assert hashlib.sha256((output / "cases.jsonl").read_bytes()).hexdigest() == manifest["suite_sha256"]
    _load_env_file(env_file)
    source_config = read_json(Path(manifest["source_run"]) / "config.resolved.json")
    # config.resolved.json stores APIConfig names, whereas the CLI loader uses
    # model_seed/max_output_tokens. Preserve the actual recorded wire values.
    params = manifest["source_api_parameters"]
    source_config["api"].update(model_seed=params.get("seed"), max_output_tokens=params["max_tokens"])
    cfg = _api_config(source_config)
    store = SQLiteEventStore(str(output / "api_calls.sqlite"))
    client = OpenAICompatibleClient(cfg, store=store)
    actual = client._request_payload("MEMORY", [])
    actual.pop("messages")
    assert actual == manifest["source_api_parameters"], "API parameter drift"
    identity = {k: v for k, v in asdict(cfg).items() if k != "api_key"}
    if (output / "api_identity.json").exists():
        assert read_json(output / "api_identity.json") == identity
    else:
        write_json(output / "api_identity.json", identity, exclusive=True)
    history = rows(output / "results.jsonl") if (output / "results.jsonl").exists() else []
    complete = {(r["case_id"], r["version"]) for r in latest_results(history)
                if not retry_api_errors or not r.get("api_error")}
    semaphore = asyncio.Semaphore(concurrency)
    write_json(output / ("schedule.stage" + "-".join(map(str, sorted(stages))) + ".json"),
               dict(stages=sorted(stages), max_concurrent_pairs=concurrency,
                    within_pair="sequential AB/BA alternating; no semantic retry"))

    async def evaluate_pair(index, case):
        async with semaphore:
            for version in (("A", "B") if index % 2 == 0 else ("B", "A")):
                if (case["case_id"], version) in complete:
                    continue
                run_id = f"memory-ab.{case['case_id']}.{version}"
                result = {k: case[k] for k in ("case_id", "parent_case_id", "stage", "sample_id", "perturbation", "expected", "input_sha256")}
                result.update(version=version, raw_output=None, api_error=None,
                              evaluation_attempt=1 + sum(r["case_id"] == case["case_id"] and r["version"] == version for r in history))
                previous_ids = {c.call_id for c in store.list_model_calls(run_id)}
                try:
                    raw = await client.complete(run_id=run_id, interface="MEMORY", messages=case["messages"][version], agent_role="HIGH")
                    result["raw_output"] = raw
                except Exception as exc:
                    result["api_error"] = str(exc)
                else:
                    result["score"] = score(case, raw)
                calls = [c for c in store.list_model_calls(run_id) if c.call_id not in previous_ids]
                result["call_ids"] = [c.call_id for c in calls]
                result["input_tokens"] = sum(c.input_tokens or 0 for c in calls if not c.cache_hit)
                result["output_tokens"] = sum(c.output_tokens or 0 for c in calls if not c.cache_hit)
                result["latency_ms"] = sum(c.latency_ms or 0 for c in calls if not c.cache_hit)
                with (output / "results.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                print(f"stage{case['stage']} {version} {case['case_id']}: {result.get('raw_output') or result.get('api_error')} | pass={result.get('score', {}).get('passed', False)}", flush=True)
    try:
        await asyncio.gather(*(evaluate_pair(index, case) for index, case in enumerate(rows(output / "cases.jsonl"))
                               if case["stage"] in stages))
        report(output)
    finally:
        store.connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "report"])
    parser.add_argument("--source-run", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage", type=int, choices=[1, 2], action="append")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    parser.add_argument("--concurrency", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--retry-api-errors", action="store_true", help="Retry transport failures only; never resample a model answer")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.source_run, args.output)
    elif args.action == "run":
        asyncio.run(run(args.output, set(args.stage or [1, 2]), args.env_file, args.concurrency, args.retry_api_errors))
    else:
        report(args.output)


if __name__ == "__main__":
    main()
