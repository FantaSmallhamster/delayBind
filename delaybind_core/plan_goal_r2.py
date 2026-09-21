"""Keep R2 plans as evidence-collection plans, matching the V5.1 contract."""

from __future__ import annotations

import re


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _intent(text: str) -> str | None:
    value = _normalized(text)
    if ((value.startswith("where ") and " born" in value) or "birthplace" in value
            or "place of birth" in value):
        return "BIRTHPLACE"
    if ((value.startswith("when ") and " born" in value) or "date of birth" in value
            or "birth date" in value):
        return "BIRTH_DATE"
    if value.startswith("where ") and (" die" in value or "death place" in value):
        return "DEATH_PLACE"
    if value.startswith("when ") and (" die" in value or "death date" in value):
        return "DEATH_DATE"
    if value.startswith("where ") and (" work" in value or "employed" in value):
        return "WORKPLACE"
    if value.startswith("how many ") or "number of" in value:
        return "COUNT"
    if re.match(r"^(are|is|do|does|did|was|were|can|could|has|have|had|will|would|should)\b", value):
        return "YES_NO"
    return None


def _terminal_computation_ids(plan, question: str) -> set[str]:
    """Find model-added terminal comparison/count nodes.

    The original V5.1 PLAN contract collects evidence and delegates a final
    comparison or calculation to ANSWER.  A terminal node is safe to remove
    only when its inputs are already produced by upstream evidence queries;
    those upstream queries (including their cardinalities) remain untouched.
    """
    parents = {parent for q in plan.queries for parent in q.depends_on}
    leaves = [q for q in plan.queries if q.id not in parents]
    expected = _intent(question)
    if expected not in {"YES_NO", "COUNT"}:
        return set()
    found = set()
    for leaf in leaves:
        if _intent(leaf.template) != expected or not leaf.depends_on:
            continue
        # Counting an already-collected set is always a final calculation.
        # A yes/no node with two or more produced inputs is a final comparison.
        if expected == "COUNT" or len(leaf.depends_on) >= 2:
            found.add(leaf.id)
    return found


def normalize_evidence_collection_plan(plan, question: str):
    """Drop only redundant final computation leaves; never rewrite upstream."""
    removable = _terminal_computation_ids(plan, question)
    if not removable:
        return plan
    queries = [q for q in plan.queries if q.id not in removable]
    if not queries:
        raise ValueError("PLAN_HAS_NO_EVIDENCE_QUERY")
    return plan.model_copy(update={"queries": queries})


# Compatibility name for callers/tests created during R2 development.  Its
# behavior now follows the original V5.1 evidence-plan responsibility.
def ensure_plan_answers_question(plan, question: str):
    return normalize_evidence_collection_plan(plan, question)
