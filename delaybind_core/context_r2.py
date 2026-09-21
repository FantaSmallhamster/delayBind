"""Raw-backed per-query review contexts and effective-only reader/answer views."""

from .member_graph_r2 import enabled, member_projection
from .navigation_r2 import (
    binding_effective,
    current_uses,
    proof_facts,
    query,
    query_projection,
    query_ready,
    use_effective,
)
from .schema_r2 import MemoryContextR2
from .schema_v52 import EvidencePackV52, digest
from .text_views_v52 import memory_view


def evidence_pack(runtime, *, qid=None, fact_ids=None, extra_refs=(), final=False):
    s = runtime.state
    selected = set(fact_ids or [])
    nodes, refs, diagnostics = [], set(extra_refs), []
    if qid is None:
        for item in s.diagnostics:
            ex = s.executions[item["query_id"]]
            if (not ex.current_binding_id and item.get("query_version") == ex.version
                    and item.get("input_signature") == ex.input_signature):
                diagnostics.append({**item, "effective": False})
                refs.update(item.get("source_refs", []))
        visible_uses = [u for u in s.uses.values() if use_effective(s, u)]
    else:
        visible_uses = [u for u in current_uses(s, qid) if u.fact_id in selected]
        for parent in query(s, qid).depends_on:
            bid = s.executions[parent].current_binding_id
            if binding_effective(s, bid):
                selected |= proof_facts(s, bid)
                refs.update(s.binding_store[bid].source_refs)
        visible_uses += [u for u in s.uses.values() if use_effective(s, u) and u.fact_id in selected
                        and u.query_id != qid]
    for u in sorted(visible_uses, key=lambda u: u.use_id):
        f = s.facts[u.fact_id]
        nodes.append({**f.model_dump(), "query_id": u.query_id, "use_id": u.use_id,
                      "use_status": u.status, "input_signature": u.input_signature})
        refs.update(f.source_refs)
        selected.add(f.fact_id)
    represented = {n["fact_id"] for n in nodes}
    for fid in sorted(selected - represented):
        f = s.facts[fid]
        nodes.append({**f.model_dump(), "query_id": qid, "use_status": "REVIEW_OVERLAY", "input_signature": "OVERLAY"})
        refs.update(f.source_refs)
    if qid is None:
        for q in s.plan.queries:
            ex = s.executions[q.id]
            if not binding_effective(s, ex.current_binding_id):
                diagnostics.append(dict(
                    query_id=q.id,
                    status=ex.status,
                    review_pending=bool(ex.blocking_review_ids),
                    effective=False,
                ))
        # Only actually reviewed records qualify as diagnostic evidence. Pending
        # candidates and old blocked binding values are never promoted to navigation.
        for u in s.uses.values():
            ex = s.executions[u.query_id]
            if (u.review_context_id and u.query_version == ex.version and u.input_signature == ex.input_signature
                    and u.status in {"HELD", "CONFLICT"}):
                diagnostics.append(dict(query_id=u.query_id, fact_id=u.fact_id, status=u.status, effective=False))
                refs.update(u.reviewed_source_refs)
        for r in s.reviews.values():
            if r.status in {"PENDING", "RUNNING", "WAITING_CONTEXT"}:
                for record_id in r.staged_review_ids.values():
                    record = s.review_records[record_id]
                    diagnostics.append(dict(query_id=r.query_id, review_id=r.review_id, fact_id=record.review.fact_id,
                                            status="REVIEW_PENDING", effective=False))
                    refs.update(record.review.checked_refs)
    nav = dict(facts=nodes, binding_ports=[p for p in s.binding_ports if p["effective"]],
               links=[l for l in s.navigation_links if l["target_fact_id"] in selected
                      and set(l["source_use_ids"]) <= {n.get("use_id") for n in nodes}])
    if enabled(s):
        nav["members"], nav["member_edges"] = member_projection(s)
        nav["collection_scope_closed"] = s.scope_closed
        for q in s.plan.queries:
            ex = s.executions[q.id]
            sessions = [r for r in s.reviews.values() if r.query_id == q.id and r.status == "DONE"
                        and r.query_version == ex.version and r.input_signature == ex.input_signature]
            if sessions:
                latest = max(sessions, key=lambda r: r.completed_revision or 0)
                for branch in latest.branches:
                    if not latest.branch_results.get(branch.branch_id):
                        diagnostics.append(dict(query_id=q.id, state="UNRESOLVED_MEMBER_BRANCH",
                                                branch_id=branch.branch_id, bound_inputs=branch.bound_inputs,
                                                effective=False))
    if s.fact_only:
        diagnostics = [{k: v for k, v in item.items() if k not in {"source_refs", "review_id"}}
                       for item in diagnostics]
    raw = [] if s.fact_only else runtime.archive.fetch_sentences(refs)
    return EvidencePackV52(navigation=nav, raw_evidence=raw, unresolved_or_conflicts=diagnostics)


def budgeted_evidence_pack(runtime, counter):
    """Deterministic view-only eviction; proofs and diagnostics are never cut."""
    pack = evidence_pack(runtime)
    cfg, state = runtime.config, runtime.state
    def fits():
        text = memory_view(pack.model_dump(mode="json"), include_raw=not state.fact_only,
                           include_sources=not state.fact_only)
        if cfg.memory_token_budget is not None:
            return counter.count(text) <= cfg.memory_token_budget
        return len(text) <= cfg.memory_char_budget
    if fits():
        return pack
    pinned = {fid for bid in state.binding_store if binding_effective(state, bid) for fid in proof_facts(state, bid)}
    pinned |= {f.fact_id for f in state.facts.values() if f.observed_window == state.read_watermark}
    nodes = pack.navigation["facts"]
    node_refs = {ref for n in nodes for ref in n["source_refs"]}
    diagnostic_refs = {r.source_ref for r in pack.raw_evidence} - node_refs
    for diagnostic in pack.unresolved_or_conflicts:
        diagnostic_refs.update(diagnostic.get("source_refs", []))
        fid = diagnostic.get("fact_id")
        if fid:
            diagnostic_refs.update(state.facts[fid].source_refs)
            for u in state.uses.values():
                if u.fact_id == fid and u.status in {"HELD", "CONFLICT"}:
                    diagnostic_refs.update(u.reviewed_source_refs)
        rid = diagnostic.get("review_id")
        if rid:
            for record in state.reviews[rid].staged_review_ids.values():
                diagnostic_refs.update(state.review_records[record].review.checked_refs)
    optional = sorted({n["fact_id"] for n in nodes} - pinned, key=lambda fid: (state.facts[fid].observed_window, fid))
    hidden = []
    for fid in optional:
        hidden.append(fid)
        pack.navigation["facts"] = [n for n in pack.navigation["facts"] if n["fact_id"] != fid]
        visible_uses = {n.get("use_id") for n in pack.navigation["facts"]}
        pack.navigation["links"] = [l for l in pack.navigation["links"] if l["target_use_id"] in visible_uses
                                    and set(l["source_use_ids"]) <= visible_uses]
        refs = diagnostic_refs | {ref for n in pack.navigation["facts"] for ref in n["source_refs"]}
        pack.raw_evidence = [r for r in pack.raw_evidence if r.source_ref in refs]
        pack.navigation["view_hidden_fact_ids"] = list(hidden)
        if fits():
            break
    # If mandatory content still cannot fit, the caller raises an explicit budget
    # failure. This never substitutes summaries for raw or modifies persisted uses.
    return pack


def build_update_context(runtime, question, refs, *, counter=None):
    pack = budgeted_evidence_pack(runtime, counter) if counter else evidence_pack(runtime)
    wm = pack.model_dump(mode="json")
    sources = [r.model_dump(mode="json") for r in runtime.archive.fetch_sentences(refs)]
    current = set(refs)
    fact_only = runtime.state.fact_only
    wm["raw_evidence"] = (
        []
        if fact_only
        else [r for r in wm["raw_evidence"] if r["source_ref"] not in current]
    )
    visible_sources = (
        []
        if fact_only
        else sorted(current | {r["source_ref"] for r in wm["raw_evidence"]})
    )
    payload = dict(
        question=question,
        state_revision=runtime.state.state_revision,
        query_graph=query_projection(runtime.state, instantiate_members=True),
        working_memory=wm,
        visible_sources=visible_sources,
        window_sources=sources,
        fact_only=fact_only,
    )
    payload["member_bindings"] = enabled(runtime.state)
    payload["context_id"] = "UC" + digest([runtime.run_id, payload])
    runtime.store.save_context_manifest(
        runtime.run_id,
        payload["context_id"],
        {"interface": "UPDATE_SNAPSHOT", **payload},
    )
    return payload


def build_memory_context(runtime, review_id, *, batch_size=None):
    s, cfg = runtime.state, runtime.config
    r = s.reviews[review_id]
    if r.status in {"DONE", "CANCELLED"} or not query_ready(s, r.query_id):
        raise ValueError("REVIEW_NOT_READY")
    fact_only = s.fact_only
    remaining = [] if fact_only else [key for key in r.required_use_ids if key not in r.staged_review_ids]
    phase = "FINAL" if fact_only else "REVIEW" if remaining else "FINAL"
    required = [] if fact_only else [s.uses[k].fact_id for k in remaining[:batch_size or cfg.candidate_batch_size]]
    current = current_uses(s, r.query_id)
    eligible = [u for u in current if u.status in {"PENDING", "CANDIDATE", "ACCEPTED"}]
    selected = ({u.fact_id for u in eligible} if fact_only else set(required))
    rejected_records = []
    if phase == "FINAL":
        selected.update(u.fact_id for u in current if u.status in {"ACCEPTED", "HELD", "CONFLICT"})
        for record_id in r.staged_review_ids.values():
            record = s.review_records[record_id]
            if record.review.verdict != "REJECT":
                selected.add(record.review.fact_id)
                if record.corrected_fact_id:
                    selected.add(record.corrected_fact_id)
            else:
                rejected_records.append(dict(fact_id=record.review.fact_id, record_hash=record.record_hash))
    if r.target_binding_id:
        selected.update(proof_facts(s, r.target_binding_id))
    pack = evidence_pack(runtime, qid=r.query_id, fact_ids=selected, extra_refs=r.extra_refs)
    # Parent proof facts remain visible in the working-memory pack, but a
    # binding_result may select only direct uses of this query instance.
    allowed_support = sorted({u.fact_id for u in eligible}) if fact_only else sorted(selected)
    allowed_reviews = [] if fact_only else sorted({u.fact_id for u in current if u.fact_id in selected})
    scanning = any(j.target_query == r.query_id and j.kind == "RECALL" and j.status not in {"DONE", "CANCELLED"}
                   for j in s.jobs.values())
    dynamic = enabled(s)
    collection_ready = dynamic or not query(s, r.query_id).requires_complete_set or s.scope_closed
    branch = next((b for b in r.branches if b.branch_id not in r.branch_results), None) if phase == "FINAL" else None
    if dynamic and phase == "FINAL" and branch is None:
        raise ValueError("MEMBER_BRANCHES_ALREADY_COMPLETED")
    ctx = MemoryContextR2(context_id="", review_id=review_id, state_revision=s.state_revision,
                         allowed_mode=r.mode, phase=phase, query_id=r.query_id, query_version=r.query_version,
                         expected_cardinality=None if dynamic else query(s, r.query_id).cardinality,
                         input_signature=r.input_signature, expected_binding_id=r.target_binding_id,
                         inbox_revision=r.inbox_revision, candidate_bucket_version=r.candidate_bucket_version,
                         required_reviews=required, allowed_review_ids=allowed_reviews, allowed_fact_ids=allowed_support,
                         visible_source_refs=[] if fact_only else [raw.source_ref for raw in pack.raw_evidence],
                         raw_hashes={} if fact_only else {raw.source_ref: raw.text_sha256 for raw in pack.raw_evidence},
                         working_memory=pack,
                         barriers=dict(scan_complete=not scanning, review_complete=not remaining,
                                       collection_scope_ready=collection_ready,
                                       can_finalize=not scanning and not remaining and collection_ready,
                                       rejected_records=rejected_records),
                         scope_closed=s.scope_closed, context_limits=dict(before=cfg.source_context_before, after=cfg.source_context_after),
                         fact_only=fact_only, member_bindings=dynamic, branch=branch)
    ctx.context_id = "MC" + digest([runtime.run_id, ctx.model_dump(mode="json")])
    return ctx


def persist_memory_context(runtime, ctx):
    runtime.store.save_context_manifest(runtime.run_id, ctx.context_id, {
        "interface": "MEMORY_SNAPSHOT", **ctx.model_dump(mode="json")})
