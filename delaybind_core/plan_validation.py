"""Semantic checks for model-generated QueryPlans."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .schema import QueryPlan


_VARIABLE = re.compile(r"^\?[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"^(?:unknown|entity|person|object|value)(?:[_-].*)?$", re.I)
_SUPPORTED_OPERATORS = {
    "DIRECT", "PATH_JOIN", "REGISTERED_RULE", "RULE", "COMPARE", "EARLIER",
    "LATER", "YOUNGER", "OLDER", "COUNT", "INTERSECTION", "UNION", "FILTER",
    "ARGMAX", "ARGMIN", "PROJECT",
}
_OPERATOR_ALIASES = {"JOIN": "PATH_JOIN"}


def _mentioned(value: str, question: str) -> bool:
    """Match a constant as a whole phrase, including short entity names."""
    return re.search(rf"(?<!\w){re.escape(value.casefold())}(?!\w)", question.casefold()) is not None


def _iter_variables(value: object):
    if isinstance(value, str):
        if _VARIABLE.match(value):
            yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_variables(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_variables(item)


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


def validate_plan(
    plan: QueryPlan,
    *,
    question: str | None = None,
    require_answer_contract: bool = True,
    require_answer_target: bool = True,
) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    pattern_ids: set[str] = set()
    variables: set[str] = set()
    pattern_variables: list[set[str]] = []
    pattern_constants: list[list[str]] = []
    for index, pattern in enumerate(plan.patterns):
        path = f"patterns[{index}]"
        if pattern.id in pattern_ids:
            issues.append(PlanIssue("DUPLICATE_PATTERN_ID", f"duplicate pattern id {pattern.id!r}", path))
        pattern_ids.add(pattern.id)
        current_variables: set[str] = set()
        current_constants: list[str] = []
        for field, value in (("subject", pattern.subject), ("object", pattern.object)):
            if _VARIABLE.match(value):
                variables.add(value)
                current_variables.add(value)
            elif value.startswith("?"):
                issues.append(PlanIssue("INVALID_VARIABLE", f"{path}.{field} has invalid variable {value!r}", f"{path}.{field}"))
            elif _PLACEHOLDER.match(value.strip()):
                issues.append(PlanIssue("INVENTED_PLACEHOLDER", f"{path}.{field} uses invented placeholder {value!r}", f"{path}.{field}"))
            else:
                current_constants.append(value.strip())
        pattern_variables.append(current_variables)
        pattern_constants.append(current_constants)
        if _VARIABLE.match(pattern.subject) and pattern.subject == pattern.object:
            issues.append(
                PlanIssue(
                    "SELF_LOOP_PATTERN",
                    f"{path} uses the same variable on both sides of relation {pattern.relation!r}",
                    path,
                )
            )
        if not pattern.relation.strip():
            issues.append(PlanIssue("EMPTY_RELATION", f"{path}.relation is empty", f"{path}.relation"))

    # Every pattern component must be reachable from at least one concrete
    # entity/value. Otherwise UPDATE has no principled starting point and can
    # scan the corpus for arbitrary facts unrelated to the question.
    parent = list(range(len(plan.patterns)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_pattern_for_variable: dict[str, int] = {}
    for index, current_variables in enumerate(pattern_variables):
        for variable in current_variables:
            previous = first_pattern_for_variable.setdefault(variable, index)
            union(index, previous)
    anchored_roots: set[int] = set()
    for index, constants in enumerate(pattern_constants):
        # Constants that are absent from the question are separately reported
        # below; they must not make an otherwise free component look anchored.
        if question is not None:
            pattern = plan.patterns[index]
            anchors = [
                value
                for key, value in pattern.qualifiers.items()
                if key in {"subject_question_anchor", "object_question_anchor"}
                and isinstance(value, str)
            ]
            if any(_mentioned(value, question) for value in [*constants, *anchors]):
                anchored_roots.add(find(index))
        elif constants:
            anchored_roots.add(find(index))
    reported_roots: set[int] = set()
    for index in range(len(plan.patterns)):
        root = find(index)
        if root not in anchored_roots and root not in reported_roots:
            issues.append(
                PlanIssue(
                    "UNANCHORED_PATTERN_COMPONENT",
                    "pattern component has no concrete entity/value anchor from the question",
                    f"patterns[{index}]",
                )
            )
            # One issue per component is enough; later members add no signal.
            reported_roots.add(root)

    # Operator outputs may introduce a derived answer variable (for example
    # ``COUNT`` -> ``?count``), so include them in the declared variable set
    # before checking operator inputs and the answer contract.
    for index, operator in enumerate(plan.operators):
        for key in ("output", "output_var", "result", "target"):
            value = operator.params.get(key)
            if not isinstance(value, str):
                continue
            if _VARIABLE.match(value):
                variables.add(value)
            elif value.startswith("?"):
                issues.append(
                    PlanIssue(
                        "INVALID_OPERATOR_OUTPUT_VARIABLE",
                        f"operators[{index}].params.{key} has invalid variable {value!r}",
                        f"operators[{index}].params.{key}",
                    )
                )

    if plan.answer_contract is None and require_answer_contract:
        issues.append(PlanIssue("MISSING_ANSWER_CONTRACT", "plan must declare an answer_contract", "answer_contract"))
    elif (
        plan.answer_contract is not None
        and require_answer_target
        and plan.answer_contract.target is None
        and plan.answer_contract.type != "BOOLEAN"
    ):
        issues.append(
            PlanIssue(
                "MISSING_ANSWER_TARGET",
                "non-boolean answer_contract must name a target variable",
                "answer_contract.target",
            )
        )
    for index, operator in enumerate(plan.operators):
        normalized = _OPERATOR_ALIASES.get(operator.type.upper(), operator.type.upper())
        if normalized not in _SUPPORTED_OPERATORS:
            issues.append(PlanIssue("UNSUPPORTED_OPERATOR", f"operator {operator.type!r} is not in the V5 registry", f"operators[{index}].type"))
        for input_index, value in enumerate(operator.inputs):
            for variable in _iter_variables(value):
                if variable not in variables:
                    issues.append(
                        PlanIssue(
                            "OPERATOR_UNKNOWN_VARIABLE",
                            f"operator input {variable!r} is not declared by any pattern",
                            f"operators[{index}].inputs[{input_index}]",
                        )
                    )
        for key, value in operator.params.items():
            if key in {"output", "output_var", "result", "target"}:
                continue
            for variable in _iter_variables(value):
                if variable not in variables:
                    issues.append(
                        PlanIssue(
                            "OPERATOR_UNKNOWN_VARIABLE",
                            f"operator parameter {variable!r} is not declared by any pattern",
                            f"operators[{index}].params.{key}",
                        )
                    )
    if plan.answer_contract and plan.answer_contract.target:
        target = plan.answer_contract.target
        if _VARIABLE.match(target) and target not in variables:
            issues.append(PlanIssue("ANSWER_TARGET_UNKNOWN_VARIABLE", f"answer target {target!r} is not declared by any pattern", "answer_contract.target"))
    if question:
        for index, pattern in enumerate(plan.patterns):
            for field, value in (("subject", pattern.subject), ("object", pattern.object)):
                if _VARIABLE.match(value) or _PLACEHOLDER.match(value.strip()):
                    continue
                anchor = pattern.qualifiers.get(f"{field}_question_anchor")
                anchored_alias = isinstance(anchor, str) and _mentioned(anchor, question)
                if not _mentioned(value, question) and not anchored_alias:
                    issues.append(PlanIssue("CONSTANT_NOT_IN_QUESTION", f"{value!r} in patterns[{index}].{field} is not mentioned in the question", f"patterns[{index}].{field}"))
    return issues


def ensure_valid_plan(
    plan: QueryPlan,
    *,
    question: str | None = None,
    require_answer_contract: bool = True,
    require_answer_target: bool = True,
) -> QueryPlan:
    issues = validate_plan(
        plan,
        question=question,
        require_answer_contract=require_answer_contract,
        require_answer_target=require_answer_target,
    )
    if any(issue.fatal for issue in issues):
        raise PlanValidationError(issues)
    return plan


def format_plan_issues(issues: list[PlanIssue]) -> str:
    return "\n".join(f"- {issue.code} at {issue.path or '<plan>'}: {issue.message}" for issue in issues)
