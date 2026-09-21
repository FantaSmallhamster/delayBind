"""Compact, read-only model view of the fact-only R2 member graph.

Durable IDs, use signatures and transaction metadata stay in the evidence pack.
The model sees request-local aliases and each proof/dependency exactly once on
its member node. This is a projection, never a mutation of runtime authority.
"""

from .text_views_v52 import display, escaped, ids


MEMBER_MEMORY_VIEW_VERSION = "r2-member-memory-compact-v1"


def _aliases(values, prefix):
    return {value: f"{prefix}{i}" for i, value in enumerate(sorted(set(values)), 1)}


def compact_member_memory_view(memory):
    nav = memory["navigation"]
    facts = nav.get("facts", [])
    members = nav["members"]
    diagnostics = memory.get("unresolved_or_conflicts", [])
    # Include references outside a local overlay without inventing their text.
    fact_ids = {f["fact_id"] for f in facts}
    fact_ids.update(fid for m in members for fid in m["direct_fact_ids"])
    fact_ids.update(d["fact_id"] for d in diagnostics if d.get("fact_id"))
    fa = _aliases(fact_ids, "F")
    ma = _aliases({m["member_id"] for m in members}
                  | {mid for m in members for mid in m["parent_member_ids"]}, "M")
    ba = _aliases([d["branch_id"] for d in diagnostics if d.get("branch_id")], "BR")

    # One stored fact may have several query-specific uses. Keep its text once,
    # but retain every visible query/status association separately.
    by_id = {f["fact_id"]: f for f in facts}
    rows = ["Fact navigation:", "\n".join(
        f"{fa[fid]} | {escaped(by_id[fid]['text'])}" for fid in sorted(by_id)) or "NONE"]
    uses = {}
    for f in facts:
        uses.setdefault(f["fact_id"], set()).add(
            (f.get("query_id") or "NONE", f.get("use_status", "UNKNOWN")))
    rows += ["", "Fact uses:", "\n".join(
        f"{fa[fid]} | " + ",".join(f"{qid}:{status}" for qid, status in sorted(uses[fid]))
        for fid in sorted(uses)) or "NONE"]

    # Query ports are routing metadata, not all-to-all member dependencies.
    # Preserve consumers that do not have a bound member yet.
    ports = {}
    for port in nav.get("binding_ports", []):
        if port.get("effective", True):
            key = (port["producer_query_id"], port["variable"])
            ports.setdefault(key, set()).add(port["consumer_query_id"])
    rows += ["", "Query dependencies:", "\n".join(
        f"{qid} {slot} -> {ids(sorted(consumers))}"
        for (qid, slot), consumers in sorted(ports.items())) or "NONE"]

    rows += ["", "Binding member nodes:",
             "facts=direct support; parents=upstream member dependencies. "
             "Follow parents for ancestor paths; equal values need not be the same member."]
    rows.append("\n".join(
        f"{ma[m['member_id']]} | {m['query_id']} {m['variable']} = {escaped(display(m['value']))}"
        f" | facts={ids(fa[fid] for fid in sorted(m['direct_fact_ids']))}"
        f" | parents={ids(ma[mid] for mid in sorted(m['parent_member_ids']))}"
        for m in sorted(members, key=lambda m: m["member_id"])) or "NONE")
    # member_edges, navigation links and lineage are derived from these direct
    # support and parent edges in a validated runtime projection. Rendering all
    # four copies adds no evidence. Never merge nodes merely by their value.

    missing_facts = fact_ids - by_id.keys()
    missing_members = ma.keys() - {m["member_id"] for m in members}
    if missing_facts:
        rows += ["", "Referenced fact text not shown in this view:",
                 ids(fa[fid] for fid in sorted(missing_facts))]
    if missing_members:
        rows += ["", "Referenced parent members not shown in this view:",
                 ids(ma[mid] for mid in sorted(missing_members))]

    rows += ["", "Collection scope closed:", display(nav.get("collection_scope_closed", False)),
             "", "Unresolved queries and conflicts:"]
    rendered = []
    for diagnostic in diagnostics:
        item = {}
        for key, value in sorted(diagnostic.items()):
            if key in {"input_signature", "query_version", "review_id", "context_id",
                       "state_revision", "record_hash", "source_refs"}:
                continue  # runtime audit fields, not answer evidence
            if key == "fact_id":
                value = fa.get(value, value)
            elif key == "branch_id":
                value = ba.get(value, value)
            # In particular, bound_inputs and reason text remain verbatim;
            # do not globally replace strings that merely resemble an ID.
            item[key] = value
        rendered.append(escaped(display(item)))
    rows.append("\n".join(rendered) or "NONE")
    if nav.get("view_hidden_fact_ids"):
        rows += ["", "Older non-proof facts omitted by view budget (still stored):",
                 str(len(nav["view_hidden_fact_ids"]))]
    return "\n".join(rows)
