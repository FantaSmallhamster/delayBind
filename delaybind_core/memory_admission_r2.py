"""Instance-scoped authorization for strict fact-only MEMORY requests."""

from .navigation_r2 import current_uses, query, query_ready, binding_effective
from .schema_v52 import digest
from .schema_r2 import UNIFIED_MEMORY_INTERFACE, UNIFIED_MEMORY_VERSION, use_key

STRICT_POLICY = "strict-recall-v1"
UNIFIED_POLICY = "unified-evidence-v1"


def strict(state):
    return state.fact_only and state.admission_policy == STRICT_POLICY


def unified(state):
    return (state.fact_only and state.memory_interface == UNIFIED_MEMORY_INTERFACE
            and state.admission_policy == UNIFIED_POLICY)


def validates_evidence(state):
    """Proof checks apply independently of the old recall admission tokens."""
    return strict(state) or unified(state)


def collect_memory_evidence(state, qid, target_binding_id=None):
    """Collect the whole query bucket plus actual prior support, without filtering.

    The semantic digest excludes mutable acceptance status and trigger labels:
    publishing a result or reading an unrelated window is not new evidence.
    Scheduling creates the current-instance uses before freezing this snapshot.
    """
    q, ex = query(state, qid), state.executions[qid]
    prior = current_support_uses(state, qid, target_binding_id)
    fact_ids = sorted(set(state.route_index.get(qid, [])) |
                      {state.uses[key].fact_id for key in prior})
    use_ids = [use_key(qid, ex.version, ex.input_signature, fid) for fid in fact_ids]
    pending = {entry.use_key for entry in state.inbox.values()
               if entry.query_id == qid and entry.status == "PENDING"}
    origins = set()
    for key in use_ids:
        if key in prior:
            origins.add("PRIOR_SUPPORT")
        if key in pending:
            origins.add("UPDATE")
        elif key not in prior:
            use = state.uses.get(key)
            origins.add("PLAN_REPAIR" if use and use.acceptance_origin == "PLAN_REPAIR" else "DEFERRED")
    signature = digest([
        UNIFIED_MEMORY_VERSION, qid, ex.version, ex.input_signature,
        q.model_dump(mode="json"),
        [(fid, state.facts[fid].text) for fid in fact_ids],
        state.scope_closed if q.requires_complete_set else None,
    ])
    return dict(use_ids=use_ids, fact_ids=fact_ids, digest=signature,
                prior_support_use_ids=prior, evidence_origins=sorted(origins),
                fact_aliases={f"F{i}": fid for i, fid in enumerate(fact_ids, 1)})


def grant_admission(state, use, origin, source_id):
    if origin not in {"UPDATE", "RECALL"} or use.status == "INVALIDATED":
        raise ValueError("INVALID_ADMISSION_ORIGIN_OR_USE")
    ex = state.executions[use.query_id]
    if (use.query_version, use.input_signature) != (ex.version, ex.input_signature):
        raise ValueError("STALE_ADMISSION_USE")
    token = digest([state.admission_policy, use.use_id, origin, source_id])
    if token == use.admission_token:
        return False
    use.admission_origin, use.admission_token = origin, token
    return True


def pending_admissions(state, qid):
    return {u.use_id: u.admission_token for u in current_uses(state, qid)
            if u.admission_origin in {"UPDATE", "RECALL"} and u.admission_token
            and u.admission_token != u.consumed_admission_token}


def current_support_uses(state, qid, target_binding_id=None):
    bid = target_binding_id if target_binding_id is not None else state.executions[qid].current_binding_id
    binding = state.binding_store.get(bid)
    if binding is None or not binding.valid or binding.producer_query_id != qid:
        return []
    ex = state.executions[qid]
    if (binding.query_version, binding.input_signature) != (ex.version, ex.input_signature):
        return []
    return sorted(key for key in binding.direct_use_ids if key in state.uses
                  and state.uses[key].status != "INVALIDATED")


def build_admission_snapshot(state, qid, target_binding_id=None):
    admitted = pending_admissions(state, qid)
    support = current_support_uses(state, qid, target_binding_id)
    use_ids = sorted(set(admitted) | set(support))
    fact_ids = sorted({state.uses[key].fact_id for key in use_ids})
    ex = state.executions[qid]
    parents = [(p, state.executions[p].current_binding_id) for p in query(state, qid).depends_on]
    signature = digest([state.admission_policy, qid, ex.version, ex.input_signature,
                        sorted(admitted.items()), support, parents])
    return dict(admitted_use_tokens=admitted, prior_support_use_ids=support,
                use_ids=use_ids, fact_ids=fact_ids, digest=signature)


def allow_upstream_inference(state, qid, branch=None):
    q = query(state, qid)
    if not (getattr(q, "allow_upstream_only", False) and q.inputs
            and any(qid in child.depends_on for child in state.plan.queries)):
        return False
    if not query_ready(state, qid):
        return False
    return branch is None or bool(branch.parent_member_ids) and all(
        member_id in {m.member_id for b in state.binding_store.values() if b.valid for m in b.members}
        for member_id in branch.parent_member_ids)


def memory_work_decision(state, qid, *, recall_pending=False, branch=None):
    if not query_ready(state, qid):
        return "WAIT_UPSTREAM"
    if recall_pending and not unified(state):
        return "WAIT_RECALL"
    snapshot = (collect_memory_evidence(state, qid) if unified(state)
                else build_admission_snapshot(state, qid))
    if snapshot["fact_ids"] or allow_upstream_inference(state, qid, branch):
        return "CALL"
    return "SKIP_NO_EVIDENCE"
