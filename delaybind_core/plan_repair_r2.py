"""Explicit HIGH PLAN(mode=REPAIR), separate from MEMORY binding authority."""

from pydantic import ValidationError

from .schema_r2 import Execution, EvidencePlanR2, PlanRepairFailureR2
from .schema_v52 import QueryPlanV3, digest
from .text_protocol_v52 import fields, refs, parse_memory as parse_legacy_patch
from .protocol_r2 import strict_lines
from .navigation_r2 import descendants, refresh, topological, query_projection, binding_effective
from .runtime_r2 import invalidate_closure
from .review_jobs_r2 import ensure_use, schedule_ready, event
from .context_r2 import evidence_pack


REJECTION_POLICY_VERSION = "optional-repair-reject-v1"


class PlanRepairProposalError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def register_repair_context(runtime, ctx):
    runtime.store.save_context_manifest(runtime.run_id, ctx["context_id"],
                                        {"interface": "PLAN_REPAIR_SNAPSHOT", **ctx})


def build_repair_context(runtime, question, *, persist=True):
    s = runtime.state
    pending = sorted(set(s.hints) - set(s.processed_hints))
    allowed = sorted(set(s.hints) | {f for bucket in s.route_index.values() for f in bucket})
    wm = evidence_pack(runtime, fact_ids=allowed).model_dump(mode="json")
    ctx = dict(mode="REPAIR", question=question, state_revision=s.state_revision, query_graph=query_projection(s),
               hint_ids=pending, allowed_fact_ids=allowed, working_memory=wm, fact_only=s.fact_only,
               member_bindings=isinstance(s.plan, EvidencePlanR2))
    ctx["context_id"] = "PC" + digest([runtime.run_id, ctx])
    if persist:
        register_repair_context(runtime, ctx)
    return ctx


def repair_basis_key(state, ctx):
    """Semantic repair basis; window progress and request identity are excluded."""
    graph = [{key: row.get(key) for key in (
        "id", "template", "output", "inputs", "status", "version", "input_signature",
        "rendered_query", "bound_inputs", "extraction_instances", "current_value",
        "effective", "review_pending") if key in row}
             for row in ctx["query_graph"]["queries"]]
    bindings = [b.model_dump(mode="json") for bid, b in sorted(state.binding_store.items())
                if binding_effective(state, bid)]
    basis = dict(plan=state.plan.model_dump(mode="json"), hint_ids=ctx["hint_ids"],
                 authorized_facts=[state.facts[fid].model_dump(mode="json", exclude={"observed_window"})
                                   for fid in ctx["allowed_fact_ids"]],
                 query_graph=graph, effective_bindings=bindings,
                 scope_closed=state.scope_closed, fact_only=state.fact_only,
                 mode=ctx["mode"], policy=REJECTION_POLICY_VERSION)
    return "PR" + digest(basis)


def parse_repair(raw, plan):
    rows = strict_lines(raw)
    if rows == ["NONE"]:
        return [], []
    head = fields(rows[0])
    if len(head) != 2 or head[0] != "REPAIR" or rows[-1] != "END REPAIR":
        raise ValueError("EXPECTED_PLAN_REPAIR_BLOCK")
    evidence = refs(head[1])
    translated = []
    upstream_flags = {}
    current_upsert = None
    upsert_has_legacy_fields = False
    for line in rows[1:-1]:
        f = fields(line)
        if f[0] == "UPSERT" and len(f) == 2:
            current_upsert = f[1]
            upsert_has_legacy_fields = False
            translated.append("PATCH | " + f[1] + " | " + (",".join(evidence) or "NONE"))
        elif line == "END UPSERT":
            if not upsert_has_legacy_fields and current_upsert in upstream_flags:
                existing = next((q for q in plan.queries if q.id == current_upsert), None)
                if existing is None:
                    raise ValueError("UPSTREAM_ONLY_PATCH_REQUIRES_EXISTING_QUERY")
                translated.append("query: " + existing.template)
            current_upsert = None
            translated.append("END PATCH")
        elif line.startswith("allow_upstream_only:"):
            value = line.split(":", 1)[1].strip().lower()
            if current_upsert is None or current_upsert in upstream_flags or value not in {"true", "false"}:
                raise ValueError("INVALID_OR_DUPLICATE_UPSTREAM_ONLY_FIELD")
            upstream_flags[current_upsert] = value == "true"
        elif f[0] in {"PATCH", "BIND", "REBIND", "ASSESS", "KEEP", "UNBIND", "FOCUS"}:
            raise ValueError("PLAN_REPAIR_FORBIDDEN_ACTION")
        else:
            if current_upsert is not None:
                upsert_has_legacy_fields = True
            translated.append(line)
    proposal = parse_legacy_patch("\n".join(translated), "plan-repair", plan=plan)
    ops = [op.model_dump(exclude_none=True) for op in proposal.operations]
    for op in ops:
        if op["op"] == "PATCH":
            if not isinstance(plan, EvidencePlanR2) and op["query_id"] in upstream_flags:
                raise ValueError("UPSTREAM_ONLY_REQUIRES_R2_MEMBER_PLAN")
            if op["query_id"] in upstream_flags:
                op["patch"]["allow_upstream_only"] = upstream_flags[op["query_id"]]
            elif isinstance(plan, EvidencePlanR2) and set(op["patch"]) & {"template", "output", "inputs", "depends_on"}:
                op["patch"]["allow_upstream_only"] = False
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
        if runtime.archive is None or runtime.archive.fetch_sentence(item["source_ref"]).text_sha256 != item["text_sha256"]:
            raise ValueError("RAW_INTEGRITY_ERROR")
    s, events, receipt_payload = _prepare_repair(runtime, raw, ctx)
    return runtime.commit(s, events, context_id=ctx["context_id"], response=response,
                          receipt=receipt_payload)


def _prepare_repair(runtime, raw, ctx):
    try:
        ops, evidence = parse_repair(raw, runtime.state.plan)
    except ValueError as exc:
        raise PlanRepairProposalError("PLAN_REPAIR_FORMAT", str(exc)) from exc
    allowed = set(ctx["allowed_fact_ids"])
    if not set(evidence) <= allowed or (ops and not evidence):
        raise PlanRepairProposalError("PLAN_REPAIR_EVIDENCE_REQUIRED")
    s, events = runtime.state.model_copy(deep=True), []
    queries = {q.id: q.model_dump() for q in s.plan.queries}
    roots, routed = set(), set()
    for op in ops:
        if op["op"] == "PATCH":
            qid = op["query_id"]
            if isinstance(s.plan, EvidencePlanR2) and "cardinality" in op["patch"]:
                raise PlanRepairProposalError("PLAN_CARDINALITY_IS_RUNTIME_OWNED")
            updated = {**queries.get(qid, {"id": qid}), **op["patch"]}
            if updated != queries.get(qid):
                roots.add(qid)
            queries[qid] = updated
    try:
        proposed = type(s.plan)(plan_id=s.plan.plan_id, queries=list(queries.values()))
    except ValidationError as exc:
        raise PlanRepairProposalError("PLAN_REPAIR_INVALID_PLAN", str(exc)) from exc
    for op in ops:
        if op["op"] == "ROUTE" and (op["query_id"] not in {q.id for q in proposed.queries}
                                      or not set(op["fact_ids"]) <= allowed):
            raise PlanRepairProposalError("PLAN_ROUTE_NOT_AUTHORIZED")
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
        if qid not in s.executions:
            raise ValueError("PLAN_REPAIR_PREPARE_INCONSISTENT")
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
    return s, events, {"affected_query_ids": sorted(affected)}


def record_plan_repair_rejection(runtime, ctx, key, error_codes, response_hashes):
    import json
    if ctx["state_revision"] != runtime.state.state_revision:
        raise ValueError("STALE_PLAN_REPAIR_CONTEXT")
    row = runtime.store.connection.execute("SELECT payload_json FROM context_manifests WHERE run_id=? AND context_id=?",
                                           (runtime.run_id, ctx["context_id"])).fetchone()
    if not row or json.loads(row[0]) != {"interface": "PLAN_REPAIR_SNAPSHOT", **ctx}:
        raise ValueError("PLAN_REPAIR_CONTEXT_NOT_REGISTERED")
    if (ctx["mode"] != "REPAIR" or key != repair_basis_key(runtime.state, ctx)
            or not error_codes or len(error_codes) != len(response_hashes)):
        raise ValueError("PLAN_REPAIR_REJECTION_BASIS_MISMATCH")
    if key in runtime.state.plan_repair_failures:
        return
    s = runtime.state.model_copy(deep=True)
    record = PlanRepairFailureR2(
        hint_ids=list(ctx["hint_ids"]), base_plan_hash=digest(s.plan.model_dump(mode="json")),
        basis_hash=key, context_id=ctx["context_id"], base_state_revision=ctx["state_revision"],
        attempt_count=len(response_hashes), error_codes=list(error_codes),
        response_hashes=list(response_hashes))
    s.plan_repair_failures[key] = record
    runtime.commit(s, [("PLAN_REPAIR_REJECTED", {"basis_hash": key,
                                                   "context_id": ctx["context_id"],
                                                   "error_codes": list(error_codes),
                                                   "attempt_count": len(response_hashes)})])
