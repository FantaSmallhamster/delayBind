"""V5.1-style prose sections, fact lines and verbatim anchored source text."""


def ids(values):
    return ",".join(str(v) for v in values) or "NONE"


def display(value):
    if value is None:
        return "NONE"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple)):
        return "; ".join(display(v) for v in value) or "EMPTY SET"
    if isinstance(value, dict):
        return "; ".join(f"{k}={display(v)}" for k, v in value.items()) or "NONE"
    return str(value)


def escaped(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")


def facts_view(facts, *, include_sources=True):
    if include_sources:
        return "\n".join(f"{f['fact_id']} | {ids(f['source_refs'])} | {escaped(f['text'])}" for f in facts) or "NONE"
    return "\n".join(f"{f['fact_id']} | {escaped(f['text'])}" for f in facts) or "NONE"


def raw_view(sources):
    # Match the cursor's budgeted format. Metadata stays in the archive/context,
    # not the model input. Preserve every raw character, including whitespace.
    return "".join(
        f"[{r['source_ref']}] {r['text']}\n"
        for r in sources) or "NONE"


def plain_text_view(sources):
    """Expose the current UPDATE text without leaking archive/source IDs."""
    if not sources:
        return "NONE"
    rows, previous_document = [], None
    for source in sources:
        # Fact-only chunk refs are runtime metadata and remain hidden.  A
        # generic separator nevertheless makes independent documents explicit
        # so the extractor cannot treat adjacency as a cross-document relation.
        source_ref = source.get("source_ref", "")
        document = source_ref.split("@C", 1)[0]
        if rows and document != previous_document:
            rows.append("\n\n<DOCUMENT_BOUNDARY>\n\n")
        rows.append(source["text"])
        previous_document = document
    return "".join(rows)


def query_targets(query):
    """Return distinct rendered targets without merging their graph proofs.

    An explicitly empty natural join is not an unresolved wildcard template.
    Only projections without instances use the legacy single-query fallback.
    """
    if "extraction_instances" in query:
        return list(dict.fromkeys(i["rendered_query"] for i in query["extraction_instances"]))
    return [query.get("rendered_query", query.get("template", ""))]


# Compatibility alias for callers using the original UPDATE-oriented name.
extraction_queries = query_targets


def queries_view(graph):
    rows = []
    for q in graph.get("queries", []):
        rendered = q.get("rendered_query", q.get("template", ""))
        template = q.get("template", rendered)
        targets = query_targets(q)
        rows.extend(f"{q['id']} [{q.get('status', 'ACTIVE')}] {target}"
                    for target in targets or ["No compatible upstream branch."])
        if template != rendered and "extraction_instances" not in q:
            rows.append(f"  query: {template}")
        rows += [f"  output: {q['output']}; depends_on: {ids(q.get('inputs', {}).values())}"]
        if not q.get("member_bindings"):
            rows.append(
                f"  cardinality: {q.get('cardinality', 'SINGLE')}; "
                f"requires_complete_set: {display(q.get('requires_complete_set', False))}"
            )
        if q.get("bound_inputs"):
            rows.append("  bound inputs: " + display(q["bound_inputs"]))
        if q.get("current_binding_id"):
            rows.append("  current binding: " + q["current_binding_id"])
        if "effective" in q:
            rows.append("  binding effective: " + display(q["effective"]))
            if q.get("current_binding_id"):
                rows.append("  current value: " + display(q.get("current_value")))
        if q.get("review_pending"):
            rows.append("  review pending: old result is blocked, not certain")
        if q.get("review_barrier"):
            rows.append("  deferred review pending; do not bind yet")
    return "\n".join(rows) or "NONE"


def extraction_targets_view(graph):
    """Render only extraction targets so runtime state cannot bias fact recall."""
    rows = []
    for q in graph.get("queries", []):
        rows.extend(f"{q['id']} | {rendered}" for rendered in query_targets(q))
    return "\n".join(rows) or "NONE"


def update_routing_view(graph):
    queries = graph.get("queries", [])
    rows = []
    for q in queries:
        consumers = [child["id"] for child in queries if q["id"] in child.get("inputs", {}).values()]
        targets = query_targets(q)
        if not targets:
            continue
        rows.extend(f"{q['id']} [{q.get('status', 'ACTIVE')}] evidence needed: {target}"
                    for target in targets)
        if consumers:
            rows.append(f"  Bridge producer for {ids(consumers)}: evidence establishing {q['output']} "
                        f"belongs to {q['id']}; do not report it only under downstream queries.")
        if q.get("status") == "DORMANT":
            rows.append("  Named candidates may be deferred; they do not establish upstream bindings.")
    return "\n".join(rows) or "NONE"


def memory_view(memory, *, include_raw=True, include_sources=True):
    nav = memory.get("navigation") or {}
    if not include_raw and not include_sources and "members" in nav:
        # Both prompt rendering and budget measurement use this same projection.
        # Legacy plans and the raw-evidence protocol retain their original IDs.
        from .member_memory_view_r2 import compact_member_memory_view
        return compact_member_memory_view(memory)
    facts = nav.get("facts", []) if isinstance(nav, dict) else []
    rows = ["Fact navigation:", facts_view(facts, include_sources=include_sources), "", "Fact uses:"]
    rows.append("\n".join(f"{n['query_id']} | {n['fact_id']} | {n['use_status']} | {n['input_signature']}" for n in facts) or "NONE")
    rows += ["", "Binding ports:"]
    rows.append("\n".join(display(p) for p in nav.get("binding_ports", [])) if isinstance(nav, dict) and nav.get("binding_ports") else "NONE")
    rows += ["", "Evidence links:"]
    rows.append("\n".join(display(link) for link in nav.get("links", [])) if isinstance(nav, dict) and nav.get("links") else "NONE")
    if "members" in nav:
        rows += ["", "Binding member nodes:", "\n".join(display(n) for n in nav["members"]) or "NONE",
                 "", "Member dependency and support edges:",
                 "\n".join(display(e) for e in nav["member_edges"]) or "NONE",
                 "", "Collection scope closed:", display(nav["collection_scope_closed"])]
    rows += ["", "Unresolved queries and conflicts:"]
    rows.append("\n".join(display(d) for d in memory.get("unresolved_or_conflicts", [])) or "NONE")
    if nav.get("view_hidden_fact_ids"):
        rows += ["", "Older non-proof facts omitted by view budget (still stored):", str(len(nav["view_hidden_fact_ids"]))]
    if include_raw:
        rows += ["", "Original evidence:", raw_view(memory.get("raw_evidence", []))]
    return "\n".join(rows)


def request_view(interface, payload):
    if interface == "MEMORY_REPAIR":
        return ("Context ID:\n" + payload["context_id"] + "\n\nValidation errors:\n" + str(payload["validation_errors"])
                + "\n\nRejected response (untrusted):\n" + payload["rejected_response"]
                + "\n\nOriginal MEMORY request:\n" + request_view("MEMORY", payload["original_memory_request"]))
    rows = ["Question:", payload["question"]]
    if interface in {"UPDATE", "UPDATE_REPAIR", "MEMORY"}:
        rows += ["", "Plan:", queries_view(payload.get("query_graph", {}))]
    if interface == "RECALL":
        rows += ["", "Query:", queries_view({"queries": [payload["query_instance"]]}),
                 "", "Deferred candidates:", facts_view(payload["candidate_batch"]),
                 "", "Selectable candidate IDs:", ids(payload["selectable_fact_ids"])]
    else:
        rows += ["", "Working memory:", memory_view(payload.get("working_memory", {}))]
    if interface in {"UPDATE", "UPDATE_REPAIR"}:
        rows += ["", "Evidence routing checklist:", update_routing_view(payload.get("query_graph", {})),
                 "", "Current window:", raw_view(payload["window_sources"])]
        if interface == "UPDATE_REPAIR":
            rows += ["", "Rejected lines and errors:", "\n".join(display(r) for r in payload["rejected_items"]),
                     "", "Allowed repair targets:",
                     "\n".join(f"{t['repair_id']} | {t['kind']} | {escaped(t['text'])}" for t in payload["repair_targets"]) or "NONE",
                     "", "Last repair errors:", "\n".join(display(r) for r in payload.get("repair_errors", [])) or "NONE",
                     "", "Already retained facts:", "\n".join(display(r) for r in payload["retained_items"]) or "NONE"]
    elif interface == "MEMORY":
        rows += ["", "Context ID:", payload["context_id"], "", "Pending fact uses:",
                 "\n".join(f"{u['query_id']} | {u['fact_id']}" for u in payload["pending_uses"]) or "NONE",
                 "", "Input signatures:", "\n".join(f"{q} | {s}" for q, s in payload["input_signatures"].items()),
                 "", "Saved plan hints:", facts_view(payload.get("plan_hints", [])),
                 "", "Visible fact IDs:", ids(payload["allowed_fact_ids"]),
                 "", "Query IDs eligible for BIND at the start of this call:", ids(payload["bindable_query_ids"]),
                 "", "Recall/review barriers:", "\n".join(f"{q}: {display(b)}" for q, b in payload["review_barriers"].items()),
                 "", "Context neighborhood limits:", display(payload["context_request_limits"]),
                 "", "Input scope closed:", display(payload["scope_closed"])]
    elif interface == "ANSWER":
        rows += ["", "Answer format:", payload["answer_contract"]]
    if payload.get("validation_errors"):
        rows += ["", "Validation errors:", str(payload["validation_errors"])]
    return "\n".join(rows)
