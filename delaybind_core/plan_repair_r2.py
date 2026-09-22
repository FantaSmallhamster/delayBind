"""Explicit HIGH PLAN(mode=REPAIR), separate from MEMORY binding authority."""

from .schema_r2 import Execution, EvidencePlanR2
from .schema_v52 import QueryPlanV3, digest
from .text_protocol_v52 import fields, refs, parse_memory as parse_legacy_patch
from .protocol_r2 import strict_lines
from .navigation_r2 import descendants, refresh, topological, query_projection
from .runtime_r2 import invalidate_closure
from .review_jobs_r2 import ensure_use, schedule_ready, event
from .context_r2 import evidence_pack


def build_repair_context(runtime, question):
    s = runtime.state
    pending = sorted(set(s.hints) - set(s.processed_hints))
    allowed = sorted(set(s.hints) | {f for bucket in s.route_index.values() for f in bucket})
    wm = evidence_pack(runtime, fact_ids=allowed).model_dump(mode="json")
    ctx = dict(mode="REPAIR", question=question, state_revision=s.state_revision, query_graph=query_projection(s),
               hint_ids=pending, allowed_fact_ids=allowed, working_memory=wm, fact_only=s.fact_only,
               member_bindings=isinstance(s.plan, EvidencePlanR2))
    ctx["context_id"] = "PC" + digest([runtime.run_id, ctx])
    runtime.store.save_context_manifest(runtime.run_id, ctx["context_id"], {"interface": "PLAN_REPAIR_SNAPSHOT", **ctx})
    return ctx


def parse_repair(raw, plan):
    rows = strict_lines(raw)
    if rows == ["NONE"]:
        return [], []
    head = fields(rows[0])
    if len(head) != 2 or head[0] != "REPAIR" or rows[-1] != "END REPAIR":
        raise ValueError("EXPECTED_PLAN_REPAIR_BLOCK")
    evidence = refs(head[1])
    translated = []
    for line in rows[1:-1]:
        f = fields(line)
        if f[0] == "UPSERT" and len(f) == 2:
            translated.append("PATCH | " + f[1] + " | " + (",".join(evidence) or "NONE"))
        elif line == "END UPSERT":
            translated.append("END PATCH")
        elif f[0] in {"PATCH", "BIND", "REBIND", "ASSESS", "KEEP", "UNBIND", "FOCUS"}:
            raise ValueError("PLAN_REPAIR_FORBIDDEN_ACTION")
        else:
            translated.append(line)
    proposal = parse_legacy_patch("\n".join(translated), "plan-repair", plan=plan)
    ops = [op.model_dump(exclude_none=True) for op in proposal.operations]
    if not ops or any(op["op"] not in {"PATCH", "ROUTE"} for op in ops):
        raise ValueError("PLAN_REPAIR_FORBIDDEN_ACTION")
    return ops, evidence


def apply_repair(runtime, raw, ctx):
    import json
    response = {"text": raw}
    receipt = runtime.store.r2_receipt(runtime.run_id, ctx["context_id"], digest(response))
    if receipt:
        return receipt
    if runtime.config.plan_repair_mode != "on_hint":
        raise ValueError("PLAN_REPAIR_DISABLED")
    if ctx["state_revision"] != runtime.state.state_revision:
        raise ValueError("STALE_PLAN_REPAIR_CONTEXT")
    row = runtime.store.connection.execute("SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
                                          (runtime.run_id, ctx["context_id"])).fetchone()
    if not row or json.loads(row[0]) != {"interface": "PLAN_REPAIR_SNAPSHOT", **ctx}:
        raise ValueError("PLAN_REPAIR_CONTEXT_NOT_REGISTERED")
    for item in ctx["working_memory"]["raw_evidence"]:
        if runtime.archive.fetch_sentence(item["source_ref"]).text_sha256 != item["text_sha256"]:
            raise ValueError("RAW_INTEGRITY_ERROR")
    ops, evidence = parse_repair(raw, runtime.state.plan)
    allowed = set(ctx["allowed_fact_ids"])
    if not set(evidence) <= allowed or (ops and not evidence):
        raise ValueError("PLAN_REPAIR_EVIDENCE_REQUIRED")
    s, events = runtime.state.model_copy(deep=True), []
    queries = {q.id: q.model_dump() for q in s.plan.queries}
    roots, routed = set(), set()
    for op in ops:
        if op["op"] == "PATCH":
            qid = op["query_id"]
            if isinstance(s.plan, EvidencePlanR2) and "cardinality" in op["patch"]:
                raise ValueError("PLAN_CARDINALITY_IS_RUNTIME_OWNED:omit_cardinality")
            updated = {**queries.get(qid, {"id": qid}), **op["patch"]}
            if updated != queries.get(qid):
                roots.add(qid)
            queries[qid] = updated
    proposed = type(s.plan)(plan_id=s.plan.plan_id, queries=list(queries.values()))
    new_projection = s.model_copy(deep=True)
    new_projection.plan = proposed
    old_roots = roots & s.executions.keys()
    affected = roots | descendants(s, old_roots) | descendants(new_projection, roots)
    invalidate_closure(s, affected & s.executions.keys(), events)
    s.plan = proposed
    for q in proposed.queries:
        if q.id not in s.executions:
            s.executions[q.id] = Execution()
        elif q.id in roots:
            s.executions[q.id].version += 1
    refresh(s)
    for op in ops:
        if op["op"] != "ROUTE":
            continue
        qid = op["query_id"]
        if qid not in s.executions or not set(op["fact_ids"]) <= allowed:
            raise ValueError("PLAN_ROUTE_NOT_AUTHORIZED")
        for fid in op["fact_ids"]:
            bucket = s.route_index.setdefault(qid, [])
            if fid not in bucket:
                bucket.append(fid)
                bucket.sort()
                s.executions[qid].evidence_revision += 1
                routed.add(qid)
                ensure_use(s, qid, fid, status="CANDIDATE", origin="PLAN_REPAIR")
                event(events, "FACT_ROUTED", query_id=qid, fact_id=fid, caused_by="PLAN_REPAIR")
    s.processed_hints = sorted(set(s.processed_hints) | set(ctx["hint_ids"]))
    for qid in topological(s):
        if qid in affected | routed:
            schedule_ready(s, qid, "PLAN_REPAIR", events, callback=runtime.config.enable_defer_callback,
                           force_review=qid in roots)
    event(events, "PLAN_REPAIRED", changed_query_ids=sorted(roots), affected_query_ids=sorted(affected))
    return runtime.commit(s, events, context_id=ctx["context_id"], response=response,
                          receipt={"affected_query_ids": sorted(affected)})
