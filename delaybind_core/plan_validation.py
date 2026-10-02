"""Semantic checks for V08 subquery scheduling and dependencies."""
from __future__ import annotations
from dataclasses import dataclass
import re
from .schema import QueryPlan

_VARIABLE = re.compile(r"^\?[A-Za-z_][A-Za-z0-9_]*$")
QUERY_VARIABLE = re.compile(r"\?[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class PlanIssue:
    code: str
    message: str
    path: str | None = None
    fatal: bool = True


class PlanValidationError(ValueError):
    def __init__(self, issues: list[PlanIssue]):
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


def format_plan_issues(issues: list[PlanIssue]) -> str:
    return "\n".join(f"- {issue.code} at {issue.path or '<plan>'}: {issue.message}" for issue in issues)


def validate_subquery_plan(plan: QueryPlan) -> list[PlanIssue]:
    """Validate scheduling dependencies without parsing query semantics."""
    issues: list[PlanIssue] = []
    queries = {query.id: query for query in plan.queries}
    producers: dict[str, str] = {}
    if len(queries) != len(plan.queries):
        issues.append(PlanIssue("DUPLICATE_QUERY_ID", "subquery ids must be unique"))
    for query in plan.queries:
        if not query.query.strip() or not query.id.strip():
            issues.append(PlanIssue("EMPTY_QUERY", "query id and text must be non-empty"))
        if not _VARIABLE.fullmatch(query.binds):
            issues.append(PlanIssue("INVALID_VARIABLE", f"invalid output variable {query.binds!r}"))
        if query.binds in producers:
            issues.append(PlanIssue("DUPLICATE_BINDING", f"multiple queries bind {query.binds}"))
        producers[query.binds] = query.id
        if len(query.depends_on) != len(set(query.depends_on)):
            issues.append(PlanIssue("DUPLICATE_DEPENDENCY", f"duplicate dependencies in {query.id}"))
        for dependency in query.depends_on:
            if dependency not in queries:
                issues.append(PlanIssue("UNKNOWN_DEPENDENCY", f"{query.id} depends on unknown query {dependency}"))

    ancestors: dict[str, set[str]] = {}
    visiting: set[str] = set()

    def visit(query_id: str) -> set[str]:
        if query_id in ancestors:
            return ancestors[query_id]
        if query_id in visiting:
            issues.append(PlanIssue("CYCLIC_DEPENDENCY", f"dependency cycle at {query_id}"))
            return set()
        visiting.add(query_id)
        result: set[str] = set()
        for dependency in queries[query_id].depends_on:
            if dependency in queries:
                result.add(dependency)
                result.update(visit(dependency))
        visiting.remove(query_id)
        ancestors[query_id] = result
        return result

    for query in plan.queries:
        dependencies = visit(query.id)
        for variable in QUERY_VARIABLE.findall(query.query):
            producer = producers.get(variable)
            if producer is None:
                issues.append(PlanIssue("UNBOUND_PLACEHOLDER", f"no query produces {variable} in {query.id}"))
            elif producer == query.id or producer not in dependencies:
                issues.append(PlanIssue("MISSING_DEPENDENCY", f"{query.id} uses {variable} without an upstream dependency on {producer}"))
    if plan.answer_query_id is not None and plan.answer_query_id not in queries:
        issues.append(PlanIssue("UNKNOWN_ANSWER_QUERY", f"unknown answer query {plan.answer_query_id}"))
    return issues


def validate_plan(plan: QueryPlan, **_compatibility_options) -> list[PlanIssue]:
    return validate_subquery_plan(plan)


def ensure_valid_plan(plan: QueryPlan, **_compatibility_options) -> QueryPlan:
    issues = validate_plan(plan)
    if any(issue.fatal for issue in issues):
        raise PlanValidationError(issues)
    return plan
