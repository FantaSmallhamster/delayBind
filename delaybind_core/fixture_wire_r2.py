"""Shared scripted wire fixtures for R2 offline tests."""

import json
import re

from .schema_v52 import QueryPlanV3


def read_fixture_raw(text):
    """Read compact anchors in controlled fixtures, not arbitrary source text.

    Production uses the structured archive, never reparses a rendered prompt.
    Fixture sources must not contain lines impersonating anchors/section labels.
    """
    matches = list(re.finditer(r"(?m)^\[(D\d+(?::[SH]\d+(?::P\d+)?|@C\d+))\] ", text))
    output = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        ref = match[1]
        kind = "fragment" if ":P" in ref else "chunk" if "@C" in ref else "heading" if ":H" in ref else "sentence"
        output.append(dict(source_ref=ref, kind=kind, complete=kind != "fragment",
                           text=text[match.end():end].removesuffix("\n")))
    return output


def fixture_response(value):
    """Encode scripted semantic choices into the real line wire protocol."""
    def esc(text):
        return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n")
    def refs(items):
        return ",".join(items) or "NONE"
    rows = []
    if "queries" in value:
        by_var = {q["output"]: q["id"] for q in value["queries"]}
        for q in value["queries"]:
            rows.append(f"{q['id']}\nquery: {q['template']}\noutput: {q['output']}\ndepends_on: {refs(list(q.get('inputs', {}).values()))}")
        return "\n\n".join(rows)
    if "facts" in value:
        rows += [f"{refs(f['query_ids'])} | {refs(f['source_refs'])} | {esc(f['text'])} | ACTIVE" for f in value["facts"]]
        rows += [f"PLAN_HINT | {refs(f['source_refs'])} | {esc(f['text'])}" for f in value["hints"]]
    elif "selected_fact_ids" in value:
        return "SELECT " + refs(value["selected_fact_ids"]) if value["selected_fact_ids"] else "NONE"
    elif "operations" in value:
        for op in value["operations"]:
            if op["op"] == "ASSESS":
                if op.get("correction"):
                    c = op["correction"]
                    rows.append(f"CORRECT | {op['query_id']} | {op['fact_id']} | {c['local_id']} | {esc(c['text'])} | {refs(c['source_refs'])} | {refs(op['checked_refs'])} | {op['reason_code']}")
                else:
                    row = f"ASSESS | {op['query_id']} | {op['fact_id']} | {op['verdict']} | {refs(op['checked_refs'])} | {op['reason_code']}"
                    if op.get("context_request"):
                        c = op["context_request"]
                        row += f" | CONTEXT | {c['anchor']} | {c['before']} | {c['after']}"
                    rows.append(row)
            elif op["op"] == "BIND":
                rows.append(f"BIND | {op['query_id']} | {esc(json.dumps(op['value'], ensure_ascii=False))} | {refs(op['support_fact_ids'])} | {op['kind']}")
            else:
                raise AssertionError(op)
    return "\n".join(rows) or "NONE"


def smoke_plan():
    return QueryPlanV3(plan_id="raw-first-smoke", queries=[
        {"id": "Q1", "template": "Who is Cindy's teacher?", "output": "?teacher", "inputs": {}},
        {"id": "Q2", "template": "Who is ?teacher's mother?", "output": "?mother", "inputs": {"?teacher": "Q1"}},
        {"id": "Q3", "template": "Where was ?mother born?", "output": "?city", "inputs": {"?mother": "Q2"}},
    ])
