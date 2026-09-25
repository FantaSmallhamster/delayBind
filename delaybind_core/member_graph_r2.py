"""Member-level proofs and branch-local evaluation for cardinality-free R2.

Query bindings are atomic publication snapshots. Members, not snapshots, are
the entity nodes; every member has its own facts and precise parent edges.
"""

import json

from .schema_r2 import (
    BindingMemberR2,
    BindingResult,
    EvidencePlanR2,
    MemberBranchR2,
    MemberResult,
)
from .schema_v52 import digest


class MemberBranchLimit(ValueError):
    pass


def enabled(state):
    return isinstance(state.plan, EvidencePlanR2)


def scalar_value(value):
    if (not isinstance(value, (str, int, float, bool)) or
            isinstance(value, str) and (not value.strip() or value.strip().upper() in
                                       {"NONE", "UNKNOWN", "NULL"} or value.lstrip().startswith("?"))):
        raise ValueError("MEMBER_REQUIRES_ONE_CONCRETE_VALUE:use_one_BOUND_line_per_member")
    # Also rejects NaN/Infinity, including values supplied by internal callers.
    json.dumps(value, allow_nan=False)
    return value


def projected_value(members):
    values = {digest(m.value): m.value for m in members}
    ordered = [values[k] for k in sorted(values)]
    return ordered[0] if len(ordered) == 1 else ordered


def branches_for(state, qid, *, limit=1024, allow_partial=False):
    """Natural join of upstream member lineages; never flatten entity values.

    UPDATE may project partial inputs for extraction; MEMORY still requires
    every upstream binding to be effective before a branch can be evaluated.
    """
    from .navigation_r2 import query, binding_effective
    q = query(state, qid)
    rows = [dict(bound_inputs={}, parent_member_ids=[], lineage={})]
    for slot, parent in sorted(q.inputs.items()):
        bid = state.executions[parent].current_binding_id
        if not binding_effective(state, bid):
            if allow_partial:
                continue
            return []
        members = state.binding_store[bid].members
        next_rows = []
        for row in rows:
            for member in members:
                lineage = {**member.lineage, parent: member.member_id}
                if any(k in row["lineage"] and row["lineage"][k] != v for k, v in lineage.items()):
                    continue
                next_rows.append(dict(bound_inputs={**row["bound_inputs"], slot: member.value},
                                      parent_member_ids=sorted(set(row["parent_member_ids"] + [member.member_id])),
                                      lineage={**row["lineage"], **lineage}))
                if len(next_rows) > limit:
                    raise MemberBranchLimit("MEMBER_BRANCH_BUDGET")
        rows = next_rows
    return [MemberBranchR2(branch_id="BR" + digest([qid, row["parent_member_ids"]]), **row) for row in rows]


def make_members(state, ctx, result):
    from .navigation_r2 import current_uses
    from .memory_admission_r2 import validates_evidence
    branch = ctx.branch
    if branch is None:
        raise ValueError("MEMBER_BRANCH_REQUIRED")
    items = result.members or [MemberResult(value=result.value, support_fact_ids=result.support_fact_ids,
                                           decision_source_refs=result.decision_source_refs)]
    grouped = {}
    uses = {u.fact_id: u.status for u in current_uses(state, ctx.query_id)}
    if not ctx.fact_only:
        for rid in state.reviews[ctx.review_id].staged_review_ids.values():
            record = state.review_records[rid]
            fid = record.corrected_fact_id or record.review.fact_id
            uses[fid] = "ACCEPTED" if record.review.verdict == "ACCEPT" else record.review.verdict
            if record.corrected_fact_id and record.corrected_fact_id != record.review.fact_id:
                uses[record.review.fact_id] = "REJECTED"
    for item in items:
        scalar_value(item.value)
        if not set(item.support_fact_ids) <= set(ctx.allowed_fact_ids) or not set(item.support_fact_ids) <= set(uses):
            raise ValueError("SUPPORT_NOT_VISIBLE")
        if not ctx.fact_only and any(uses[f] != "ACCEPTED" for f in item.support_fact_ids):
            raise ValueError("SUPPORT_NOT_ACCEPTED")
        if ctx.fact_only and item.decision_source_refs:
            raise ValueError("FACT_ONLY_RESULT_HAS_SOURCE_REFS")
        if not set(item.decision_source_refs) <= set(ctx.visible_source_refs):
            raise ValueError("MEMBER_SOURCE_NOT_VISIBLE")
        if not item.support_fact_ids and not branch.parent_member_ids:
            raise ValueError("MISSING_MEMBER_PROOF")
        if not item.support_fact_ids and not any(ctx.query_id in q.depends_on for q in state.plan.queries):
            raise ValueError("INFERRED_ONLY_FOR_INTERMEDIATE_QUERY")
        if validates_evidence(state) and not item.support_fact_ids and not ctx.upstream_only_allowed:
            raise ValueError("UPSTREAM_ONLY_NOT_AUTHORIZED")
        key = digest(item.value)
        if key not in grouped:
            grouped[key] = (item.value, set(), set())
        grouped[key][1].update(item.support_fact_ids)
        grouped[key][2].update(item.decision_source_refs)
    members = []
    for value, facts, sources in grouped.values():
        facts = sorted(facts)
        mid = "M" + digest([ctx.query_id, ctx.query_version, branch.branch_id, value, facts, sorted(sources)])
        members.append(BindingMemberR2(member_id=mid, branch_id=branch.branch_id, value=value,
                                      direct_fact_ids=facts, parent_member_ids=branch.parent_member_ids,
                                      lineage=branch.lineage, decision_source_refs=sorted(sources)))
    return sorted(members, key=lambda m: m.member_id)


def stage_branch(state, ctx, result, session, events):
    """Stage a branch; return the aggregate only after all branches completed.

    A NOOP retains that branch's previous members. BOUND is its full supported
    snapshot (including retained members). An explicit raw-mode UNBOUND may
    withdraw that branch after review, matching the existing raw contract.
    """
    from .review_jobs_r2 import event
    from .memory_admission_r2 import unified
    if ctx.branch is None or ctx.branch not in session.branches or ctx.branch.branch_id in session.branch_results:
        raise ValueError("STALE_MEMBER_BRANCH")
    previous = state.binding_store.get(session.target_binding_id)
    if result.state == "BOUND":
        members = make_members(state, ctx, result)
    elif ctx.fact_only or result.state == "NOOP":
        members = [m for m in previous.members if m.branch_id == ctx.branch.branch_id] if previous else []
    else:
        members = []
    if unified(state):
        if result.state not in {"BOUND", "NOOP"} or result.reason_code != (
                "SUPPORTED_RESULT" if result.state == "BOUND" else "UNSUPPORTED_RESULT"):
            raise ValueError("UNIFIED_RESULT_CONTRACT_MISMATCH")
        event(events, "MEMORY_RESULT_EVALUATED", query_id=ctx.query_id, review_id=session.review_id,
              context_id=ctx.context_id, branch_id=ctx.branch.branch_id,
              memory_interface=ctx.memory_interface, evidence_digest=ctx.evidence_digest,
              evidence_origins=session.evidence_origins, unknown_result=result.state == "NOOP",
              supported_member_count=len(members) if result.state == "BOUND" else 0,
              support_fact_count=len({fid for member in members for fid in member.direct_fact_ids})
              if result.state == "BOUND" else 0)
    session.branch_results[ctx.branch.branch_id] = members
    session.branch_decisions[ctx.branch.branch_id] = result.state
    event(events, "MEMBER_BRANCH_STAGED", query_id=ctx.query_id, branch_id=ctx.branch.branch_id,
          member_ids=[m.member_id for m in members], decision=result.state)
    if len(session.branch_results) < len(session.branches):
        for job in state.jobs.values():
            if job.review_id == session.review_id and job.kind == "MEMORY" and job.status not in {"DONE", "CANCELLED"}:
                job.status, job.lease = "PENDING", None
        return None, []
    merged = sorted((m for members in session.branch_results.values() for m in members), key=lambda m: m.member_id)
    if ctx.fact_only and all(decision in {"NOOP", "UNBOUND"} for decision in session.branch_decisions.values()):
        return result.model_copy(update=dict(state="NOOP", value=None, members=[], support_fact_ids=[])), []
    if not merged:
        return result.model_copy(update=dict(state="NOOP" if ctx.fact_only else "UNBOUND", value=None,
                                              members=[], support_fact_ids=[])), []
    support = sorted({f for m in merged for f in m.direct_fact_ids})
    # Raw source coverage is checked again by the normal transaction publisher.
    sources = sorted({ref for f in support for ref in state.facts[f].source_refs}
                     | {ref for m in merged for ref in m.decision_source_refs})
    return BindingResult(state="BOUND", value=projected_value(merged),
                         kind="DIRECT" if support else "INFERRED", support_fact_ids=support,
                         decision_source_refs=sources, reason_code="MEMBERS_SUPPORTED",
                         reason="Runtime assembled branch-local member proofs."), merged


def member_projection(state):
    from .navigation_r2 import binding_effective
    nodes, edges = [], []
    for binding in state.binding_store.values():
        if not binding_effective(state, binding.binding_id):
            continue
        for m in binding.members:
            nodes.append(dict(**m.model_dump(), query_id=binding.producer_query_id,
                              binding_id=binding.binding_id, variable=binding.variable, effective=True))
            for fid in m.direct_fact_ids:
                edges.append(dict(kind="FACT_SUPPORT", source_fact_id=fid, target_member_id=m.member_id))
            for parent in m.parent_member_ids:
                edges.append(dict(kind="MEMBER_DEPENDENCY", source_member_id=parent, target_member_id=m.member_id))
    return sorted(nodes, key=lambda n: n["member_id"]), sorted(edges, key=digest)


def assert_member_invariants(state):
    from .navigation_r2 import query
    from .memory_admission_r2 import unified
    # Include blocked but still current snapshots: barriers temporarily hide
    # proofs; they must not alter their immutable structure.
    members = {m.member_id: (b, m) for b in state.binding_store.values() if b.valid for m in b.members}
    for b in state.binding_store.values():
        if not b.valid:
            continue
        if not b.members or digest(b.value) != digest(projected_value(b.members)):
            raise ValueError("MEMBER_VALUE_PROJECTION_MISMATCH")
        if set(b.direct_fact_ids) != {f for m in b.members for f in m.direct_fact_ids}:
            raise ValueError("MEMBER_FACT_PROJECTION_MISMATCH")
        if len({m.member_id for m in b.members}) != len(b.members):
            raise ValueError("DUPLICATE_MEMBER_NODE")
        planned_query = query(state, b.producer_query_id)
        parents = set(planned_query.depends_on)
        for m in b.members:
            scalar_value(m.value)
            actual, lineage = set(), {}
            for mid in m.parent_member_ids:
                if mid not in members:
                    raise ValueError("RETIRED_PARENT_MEMBER")
                parent_binding, parent = members[mid]
                if parent_binding.binding_id not in b.parent_binding_ids:
                    raise ValueError("MEMBER_PARENT_BINDING_MISMATCH")
                pid = parent_binding.producer_query_id
                actual.add(pid)
                assignments = {**parent.lineage, pid: mid}
                if any(k in lineage and lineage[k] != v for k, v in assignments.items()):
                    raise ValueError("INCOMPATIBLE_MEMBER_LINEAGE")
                lineage.update(assignments)
            if actual != parents or lineage != m.lineage:
                raise ValueError("MEMBER_LINEAGE_MISMATCH")
            if not m.direct_fact_ids and not m.parent_member_ids:
                raise ValueError("MISSING_MEMBER_PROOF")
            if (unified(state) and not m.direct_fact_ids
                    and not (planned_query.allow_upstream_only and planned_query.inputs
                             and any(planned_query.id in child.depends_on for child in state.plan.queries))):
                raise ValueError("UPSTREAM_ONLY_NOT_AUTHORIZED")
