"""Build actual visible snapshots, then derive permissions from the rendered raw."""

from .navigation_graph import query_projection
from .schema_v52 import MemoryContext, digest, use_key
from .subqueries_v52 import current_uses
from .working_memory_v52 import render_fact_memory


def build_memory_context(runtime, query_id, *, batch_size=16, before=1, after=1):
    state = runtime.state
    uses = current_uses(state, query_id)
    pending = sorted((u for u in uses if u.status == "PENDING"), key=lambda u: u.fact_id)[:batch_size]
    keys = {use_key(u.query_id, u.input_signature, u.fact_id) for u in pending}
    hints = [state.facts[f] for f in state.hints]
    refs = set(state.extra_context_refs.get(query_id, []))
    refs.update(r for h in hints for r in h.source_refs)
    wm = render_fact_memory(state, runtime.archive, pending_keys=keys, extra_refs=refs, query_id=query_id)
    jobs = [j for j in state.recall_jobs.values() if j.query_id == query_id and
            j.input_signature == state.executions[query_id].input_signature and j.stage != "INVALIDATED"]
    scanning = any(j.stage in {"SCAN", "SCAN_INCOMPLETE"} for j in jobs)
    last_batch = len(pending) == sum(u.status == "PENDING" for u in uses)
    bindable = not scanning and last_batch and state.executions[query_id].status != "DORMANT"
    allowed = {n["fact_id"] for n in wm.navigation["facts"]}
    allowed.update(n["fact_id"] for n in wm.unresolved_or_conflicts if "fact_id" in n)
    allowed.update(state.hints)
    ctx = MemoryContext(
        context_id="", state_revision=state.state_revision, query_graph=query_projection(state),
        working_memory=wm, pending_uses=[{"query_id": u.query_id, "fact_id": u.fact_id} for u in pending],
        plan_hints=hints, allowed_fact_ids=sorted(allowed),
        allowed_source_refs=[r.source_ref for r in wm.raw_evidence],
        bindable_query_ids=[query_id] if bindable else [],
        input_signatures={query_id: state.executions[query_id].input_signature},
        review_barriers={query_id: {"scan_complete": not scanning, "last_review_batch": last_batch,
                                   "jobs": [j.model_dump() for j in jobs]}},
        scope_closed=state.scope_closed, context_request_limits={"before": before, "after": after},
    )
    ctx.context_id = "MC" + digest([runtime.run_id, ctx.model_dump(mode="json")])
    runtime.store.save_context_manifest(runtime.run_id, ctx.context_id, {
        "context_id": ctx.context_id, "interface": "MEMORY", "state_revision": ctx.state_revision,
        "context_hash": digest(ctx.model_dump(mode="json")), "visible_fact_ids": ctx.allowed_fact_ids,
        "visible_source_refs": ctx.allowed_source_refs,
        "raw_hashes": {r.source_ref: r.text_sha256 for r in wm.raw_evidence},
    })
    return ctx


def build_answer_context(runtime):
    return render_fact_memory(runtime.state, runtime.archive, final=True)


def build_update_context(runtime, question, window_refs):
    """Expose the active, raw-backed view, never the archive/defer collection."""
    memory = render_fact_memory(runtime.state, runtime.archive, reader=True).model_dump(mode="json")
    sources = [r.model_dump(mode="json") for r in runtime.archive.fetch_sentences(window_refs)]
    # Current-window text is rendered once, under Current window; the navigation
    # still points at that same visibly displayed source.
    current = set(window_refs)
    memory["raw_evidence"] = [r for r in memory["raw_evidence"] if r["source_ref"] not in current]
    visible = sorted(current | {r["source_ref"] for r in memory["raw_evidence"]})
    return {"question": question, "query_graph": runtime.query_projection(), "working_memory": memory,
            "visible_sources": visible, "window_sources": sources}
