"""V5.1-style model wire format; versioned objects remain internal only."""

import json
import re

from .fact_protocol import parse_plan as parse_legacy_plan
from .schema_v52 import QueryPlanV3
from .memory_protocol_v52 import MemoryProposal
from .fact_protocol_v52 import RecallSelection


def lines(raw):
    raw = raw.strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return [line.strip() for line in raw.splitlines() if line.strip()]


def fields(line):
    parts, current, index = [], [], 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line) and line[index + 1] in "n|\\":
            index += 1
            current.append({"n": "\n", "|": "|", "\\": "\\"}[line[index]])
        elif char == "|":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    return parts + ["".join(current).strip()]


def refs(value):
    return [] if value in {"NONE", ""} else [v.strip() for v in value.split(",") if v.strip()]


def value(text):
    # As in V5.1, only individual typed values may use JSON scalar/list syntax.
    try:
        return json.loads(text)
    except ValueError:
        return text


def parse_plan(raw):
    if raw.lstrip().startswith("{"):
        raise ValueError("PLAN requires query blocks, not a JSON document")
    cardinalities, current, retained = {}, None, []
    for line in lines(raw):
        if re.fullmatch(r"Q[0-9]+", line):
            current = line
        if line.startswith("cardinality:"):
            if current is None or current in cardinalities:
                raise ValueError("INVALID_CARDINALITY_FIELD")
            cardinalities[current] = line.split(":", 1)[1].strip()
        else:
            retained.append(line)
    legacy = parse_legacy_plan("\n".join(retained))
    producers = {q.output: q.id for q in legacy.queries}
    queries = []
    for q in legacy.queries:
        variables = set(re.findall(r"\?[A-Za-z_][A-Za-z_0-9]*", q.template))
        if not variables <= producers.keys():
            raise ValueError("UNKNOWN_INPUT_VARIABLE")
        inputs = {v: producers[v] for v in sorted(variables)}
        if set(inputs.values()) != set(q.depends_on):
            raise ValueError("DEPENDENCY_TEMPLATE_MISMATCH")
        queries.append(dict(id=q.id, template=q.template, output=q.output, inputs=inputs,
                            cardinality=cardinalities.get(q.id, "SET" if q.requires_complete_set else "SINGLE"),
                            requires_complete_set=q.requires_complete_set))
    return QueryPlanV3(plan_id=legacy.plan_id, queries=queries)


def parse_selection(raw):
    if raw.strip() == "NONE":
        return RecallSelection(selected_fact_ids=[])
    match = re.fullmatch(r"SELECT\s*(?:\|\s*)?(.+)", raw.strip())
    if not match:
        raise ValueError("RECALL requires SELECT followed by fact IDs, or NONE")
    return RecallSelection(selected_fact_ids=refs(match[1]))


def parse_memory(raw, context_id, *, plan=None):
    operations = []
    patch = None
    for line in lines(raw):
        if patch is not None:
            if line == "END PATCH":
                if not patch["patch"]:
                    raise ValueError("EMPTY_PATCH")
                operations.append(patch)
                patch = None
                continue
            key, separator, val = line.partition(":")
            key, val = key.strip(), val.strip()
            key = {"query": "template"}.get(key, key)
            if not separator or not val or key not in {"template", "output", "depends_on", "cardinality", "requires_complete_set"} or key in patch["patch"]:
                raise ValueError("INVALID_OR_DUPLICATE_PATCH_FIELD")
            if key == "depends_on":
                val = refs(val)
            elif key == "requires_complete_set":
                if val not in {"true", "false"}:
                    raise ValueError("INVALID_PATCH_BOOLEAN")
                val = val == "true"
            patch["patch"][key] = val
            continue
        if line == "NONE" and len(lines(raw)) == 1:
            break
        f = fields(line)
        op = f[0]
        if op == "ASSESS" and len(f) in {6, 10}:
            item = dict(op=op, query_id=f[1], fact_id=f[2], verdict=f[3], checked_refs=refs(f[4]), reason_code=f[5])
            if len(f) == 10:
                if f[6] == "CONTEXT":
                    item["context_request"] = dict(anchor=f[7], before=int(f[8]), after=int(f[9]))
                else:
                    raise ValueError("Invalid ASSESS extension")
        elif op == "CORRECT" and len(f) == 8:
            item = dict(op="ASSESS", query_id=f[1], fact_id=f[2], verdict="ACCEPT", checked_refs=refs(f[6]),
                        reason_code=f[7], correction=dict(local_id=f[3], text=f[4], source_refs=refs(f[5])))
        elif op == "BIND" and len(f) in {4, 5}:
            item = dict(op=op, query_id=f[1], value=value(f[2]), support_fact_ids=refs(f[3]),
                        kind=f[4] if len(f) == 5 else "DIRECT")
        elif op == "UNBIND" and len(f) in {2, 4}:
            item = dict(op=op, query_id=f[1], reason_code=f[2] if len(f) == 4 else "SUPPORT_NO_LONGER_VALID",
                        evidence_fact_ids=refs(f[3]) if len(f) == 4 else [])
        elif op in {"KEEP", "FOCUS"} and len(f) == 2:
            item = dict(op="FOCUS", fact_ids=refs(f[1]))
        elif op == "ROUTE" and len(f) == 3:
            item = dict(op=op, query_id=f[1], fact_ids=refs(f[2]))
        elif op == "PATCH" and len(f) in {2, 3}:
            if len(f) == 3 and not all(re.fullmatch(r"F[A-Za-z0-9_-]+", v) for v in refs(f[2])):
                raise ValueError("PATCH requires a text block; header contains only evidence fact IDs")
            patch = dict(op=op, query_id=f[1], patch={}, evidence_fact_ids=refs(f[2]) if len(f) == 3 else [])
            continue
        else:
            raise ValueError(f"Invalid MEMORY command: {line}")
        operations.append(item)
    if patch is not None:
        raise ValueError("UNCLOSED_PATCH: expected END PATCH")
    patches = [op for op in operations if op["op"] == "PATCH"]
    if patches:
        if plan is None:
            raise ValueError("PATCH_REQUIRES_CURRENT_PLAN")
        queries = {q.id: q.model_dump() for q in plan.queries}
        for op in patches:
            queries[op["query_id"]] = {**queries.get(op["query_id"], {"id": op["query_id"]}), **op["patch"]}
        outputs = [q.get("output") for q in queries.values()]
        if None in outputs or len(set(outputs)) != len(outputs):
            raise ValueError("PATCH_MISSING_OR_DUPLICATE_OUTPUT")
        producers = {q["output"]: qid for qid, q in queries.items()}
        for op in patches:
            q = queries[op["query_id"]]
            if not q.get("template"):
                raise ValueError("PATCH_MISSING_QUERY")
            variables = set(re.findall(r"\?[A-Za-z_][A-Za-z_0-9]*", q["template"]))
            if not variables <= producers.keys():
                raise ValueError("PATCH_UNKNOWN_VARIABLE")
            inputs = {v: producers[v] for v in sorted(variables)}
            declared = op["patch"].pop("depends_on", None)
            if declared is not None and set(declared) != set(inputs.values()):
                raise ValueError("PATCH_DEPENDENCY_MISMATCH")
            if "template" in op["patch"] or declared is not None or op["query_id"] not in {q.id for q in plan.queries}:
                op["patch"]["inputs"] = inputs
            queries[op["query_id"]].pop("depends_on", None)
            queries[op["query_id"]].update(op["patch"])
        type(plan)(plan_id=plan.plan_id, queries=list(queries.values()))
    if not raw.strip():
        raise ValueError("Empty MEMORY response; use NONE explicitly")
    return MemoryProposal(schema_version="memory-v5.2", context_id=context_id, operations=operations)
