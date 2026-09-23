"""Validate V5.1 PLAN structure and adapt it to R2 member inputs."""

from __future__ import annotations

import re

from .fact_protocol import parse_plan as parse_v51_plan
from .plan_validation import ensure_valid_plan
from .schema_r2 import EvidencePlanR2
from .plan_text_r2 import strip_upstream_only


def _dependency_ids(values: set[str]) -> str:
    return ",".join(sorted(values)) or "NONE"


def parse_v51_member_plan(raw: str, question: str) -> EvidencePlanR2:
    """Use V5.1 PLAN parsing/validation, then adapt evidence hops to R2.

    R2's member graph substitutes only variables explicitly present in a
    query. Dependencies must match those producer inputs exactly; reject
    mismatches with a specific repair error instead of rewriting them.
    No query is classified, removed, or rewritten by semantic heuristics.
    """
    legacy_text, upstream_flags = strip_upstream_only(raw)
    legacy = ensure_valid_plan(parse_v51_plan(legacy_text))
    producers = {query.output: query.id for query in legacy.queries}
    ids = {query.id: f"Q{index}" for index, query in enumerate(legacy.queries, start=1)}
    queries = []
    for query in legacy.queries:
        variables = set(re.findall(r"\?[A-Za-z_][A-Za-z_0-9]*", query.template))
        inputs = {var: producers[var] for var in sorted(variables)}
        declared = set(query.depends_on)
        expected = set(inputs.values())
        if declared != expected:
            sources = ", ".join(f"{var} from {inputs[var]}" for var in sorted(inputs)) or "NONE"
            raise ValueError(
                f"PLAN_DEPENDENCY_TEMPLATE_MISMATCH:{query.id}: "
                f"query variables and producers: {sources}; "
                f"declared depends_on: {_dependency_ids(declared)}; "
                f"expected depends_on: {_dependency_ids(expected)}; "
                f"unused dependencies: {_dependency_ids(declared - expected)}; "
                f"missing direct dependencies: {_dependency_ids(expected - declared)}. "
                f"Set depends_on to {_dependency_ids(expected)}."
            )
        queries.append(dict(id=ids[query.id], template=query.template, output=query.output,
                            inputs={var: ids[parent] for var, parent in inputs.items()},
                            requires_complete_set=query.requires_complete_set,
                            allow_upstream_only=upstream_flags.get(query.id, False)))
    return EvidencePlanR2(plan_id=legacy.plan_id, queries=queries)
