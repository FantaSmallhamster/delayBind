"""Compile legacy QueryPlan envelopes into executable open query graphs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schema import (
    OpenQueryGraph,
    QueryEdge,
    QueryNode,
    QueryNodeKind,
    QueryOperatorNode,
    QueryPlan,
)
from .relation_semantics import relation_signature


def _is_variable(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("?")


def _node_id(symbol: str) -> str:
    prefix = "var" if _is_variable(symbol) else "const"
    canonical = symbol.strip() if _is_variable(symbol) else symbol.casefold().strip()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _string_symbols(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [symbol for item in value for symbol in _string_symbols(item)]
    if isinstance(value, dict):
        return [symbol for item in value.values() for symbol in _string_symbols(item)]
    return []


def _infer_answer_target(plan: QueryPlan) -> str | None:
    if plan.answer_contract and plan.answer_contract.target:
        return plan.answer_contract.target
    operator_outputs = [
        operator.params.get(key)
        for operator in plan.operators
        for key in ("output", "output_var", "result", "target")
        if _is_variable(operator.params.get(key))
    ]
    if operator_outputs:
        return str(operator_outputs[-1])
    subjects = {pattern.subject for pattern in plan.patterns if _is_variable(pattern.subject)}
    terminal_objects = [
        pattern.object
        for pattern in plan.patterns
        if _is_variable(pattern.object) and pattern.object not in subjects
    ]
    unique = list(dict.fromkeys(terminal_objects))
    return unique[0] if len(unique) == 1 else None


def compile_open_query_graph(plan: QueryPlan) -> OpenQueryGraph:
    """Deduplicate pattern endpoints into nodes and preserve edge topology."""
    answer_target = _infer_answer_target(plan)
    nodes: dict[str, QueryNode] = {}

    def merge_type(existing: str | None, incoming: str | None) -> str | None:
        if existing is None or existing == "ENTITY":
            return incoming or existing
        if incoming is None or incoming == "ENTITY" or incoming == existing:
            return existing
        refinements = {
            ("AGENT", "PERSON"): "PERSON",
            ("PERSON", "AGENT"): "PERSON",
            ("CREATIVE_WORK", "FILM"): "FILM",
            ("FILM", "CREATIVE_WORK"): "FILM",
        }
        return refinements.get((existing, incoming), existing)

    def ensure_node(symbol: str, value_type: str | None = None) -> str:
        node_id = _node_id(symbol)
        if node_id not in nodes:
            if _is_variable(symbol):
                kind = QueryNodeKind.ANSWER if symbol == answer_target else QueryNodeKind.VARIABLE
            else:
                kind = QueryNodeKind.CONSTANT
            nodes[node_id] = QueryNode(
                id=node_id,
                kind=kind,
                symbol=symbol,
                value_type=value_type,
            )
        elif value_type:
            nodes[node_id] = nodes[node_id].model_copy(
                update={"value_type": merge_type(nodes[node_id].value_type, value_type)}
            )
        return node_id

    specs = {
        " ".join((spec.relation_family or "").casefold().split()): spec
        for spec in plan.relation_specs
        if spec.relation_family
    }
    edges: list[QueryEdge] = []
    for pattern in plan.patterns:
        signature = relation_signature(pattern.relation_key)
        spec = specs.get(" ".join(pattern.relation_key.casefold().split()))
        subject_type = spec.subject_type if spec and spec.subject_type else signature.subject_type
        object_type = spec.object_type if spec and spec.object_type else signature.object_type
        edges.append(
            QueryEdge(
                id=pattern.id,
                subject_node_id=ensure_node(pattern.subject, subject_type),
                relation=pattern.relation,
                object_node_id=ensure_node(pattern.object, object_type),
                relation_family=pattern.relation_family,
                cardinality=pattern.cardinality,
                required=pattern.required,
                qualifiers=pattern.qualifiers,
            )
        )

    operator_nodes: list[QueryOperatorNode] = []
    for operator in plan.operators:
        input_ids = [ensure_node(value) for value in _string_symbols(operator.inputs)]
        output = next(
            (
                operator.params.get(key)
                for key in ("output", "output_var", "result", "target")
                if isinstance(operator.params.get(key), str)
            ),
            None,
        )
        output_id = ensure_node(output) if output is not None else None
        operator_nodes.append(
            QueryOperatorNode(
                id=operator.id,
                operator_type=operator.type,
                input_node_ids=input_ids,
                output_node_id=output_id,
                params=operator.params,
            )
        )

    if answer_target is not None:
        answer_node_id = ensure_node(answer_target)
    else:
        answer_node_id = "answer:lambda"
        nodes[answer_node_id] = QueryNode(
            id=answer_node_id,
            kind=QueryNodeKind.ANSWER,
            symbol="lambda_answer",
            value_type=(plan.answer_contract.type if plan.answer_contract else None),
            metadata={"selection": "EVIDENCE_MODEL"},
        )

    canonical = {
        "plan_id": plan.plan_id,
        "nodes": [node.model_dump(mode="json") for node in nodes.values()],
        "edges": [edge.model_dump(mode="json") for edge in edges],
        "operators": [node.model_dump(mode="json") for node in operator_nodes],
    }
    graph_hash = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return OpenQueryGraph(
        graph_id=f"oqg-{graph_hash}",
        nodes=list(nodes.values()),
        edges=edges,
        operator_nodes=operator_nodes,
        answer_node_id=answer_node_id,
        constraints=plan.constraints,
    )
