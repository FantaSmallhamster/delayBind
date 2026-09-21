"""Scripted, offline protocol fixture. Not an oracle available to production models."""

import json
import re

from .data import build_manifest, canonicalize_record
from .runner import RunnerConfig, V5Runner
from .schema_v52 import QueryPlanV3
from .storage import SQLiteEventStore


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


def read_fixture_request(messages):
    """Test-only reader for the visible prose; no hidden payload accompanies it."""
    from .text_protocol_v52 import fields, refs
    content = messages[-1]["content"]
    if "Input data:\n" not in content:
        return {"question": content.split("Question:\n", 1)[1].split("\n\n", 1)[0]}
    body = content.split("Input data:\n", 1)[1]
    if "Original MEMORY request:\n" in body:
        prefix, original = body.split("Original MEMORY request:\n", 1)
        return {"context_id": prefix.split("Context ID:\n", 1)[1].split("\n", 1)[0],
                "original_memory_request": read_fixture_request([{"content": "Input data:\n" + original}])}

    def section(label):
        match = re.search(r"(?:^|\n\n)" + re.escape(label) + r":\n(.*?)(?=\n\n[A-Z][A-Za-z /()-]+:\n|\Z)", body, re.S)
        return match[1] if match else "NONE"

    def facts(text):
        out = []
        for line in text.splitlines():
            f = fields(line)
            if len(f) == 3 and f[0].startswith("F"):
                out.append(dict(fact_id=f[0], source_refs=refs(f[1]), text=f[2]))
        return out

    data = {"question": section("Question")}
    if "\n\nAnswer format:\n" in body:
        data["answer_contract"] = section("Answer format")
    if "\n\nWorking memory:\n" in body:
        data["working_memory"] = {"navigation": {"facts": facts(section("Fact navigation")), "links": [], "binding_ports": []},
                                  "raw_evidence": read_fixture_raw(section("Original evidence")), "unresolved_or_conflicts": []}
        # Fact navigation immediately follows the Working memory heading.
        if not data["working_memory"]["navigation"]["facts"]:
            data["working_memory"]["navigation"]["facts"] = facts(section("Working memory").removeprefix("Fact navigation:\n"))
        uses = [fields(line) for line in section("Fact uses").splitlines() if "|" in line]
        for f in data["working_memory"]["navigation"]["facts"]:
            use = next((u for u in uses if len(u) == 4 and u[1] == f["fact_id"]), None)
            if use:
                f.update(query_id=use[0], use_status=use[2], input_signature=use[3])
    if "\n\nCurrent window:\n" in body:
        window = read_fixture_raw(section("Current window"))
        memory_raw = data.get("working_memory", {}).get("raw_evidence", [])
        data.update(window_sources=window, visible_sources=sorted({r["source_ref"] for r in window + memory_raw}))
    if "\n\nDeferred candidates:\n" in body:
        data.update(candidate_batch=facts(section("Deferred candidates")), selectable_fact_ids=refs(section("Selectable candidate IDs")))
    if "\n\nPending fact uses:\n" in body:
        data.update(context_id=section("Context ID"), query_graph={"queries": []},
                    pending_uses=[dict(query_id=f[0], fact_id=f[1]) for f in (fields(l) for l in section("Pending fact uses").splitlines()) if len(f) == 2],
                    input_signatures=dict(fields(l) for l in section("Input signatures").splitlines() if "|" in l),
                    bindable_query_ids=refs(section("Query IDs eligible for BIND at the start of this call")),
                    plan_hints=facts(section("Saved plan hints")), allowed_fact_ids=refs(section("Visible fact IDs")),
                    allowed_source_refs=[r["source_ref"] for r in data["working_memory"]["raw_evidence"]], review_barriers={},
                    context_request_limits={"before": 1, "after": 1}, scope_closed=section("Input scope closed") == "true")
    return data


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


class ScriptedSmokeClient:
    def __init__(self, *, corrupt_extraction=False):
        self.calls = []
        self.corrupt_extraction = corrupt_extraction

    async def complete(self, *, interface, messages, **kwargs):
        assert kwargs.get("response_schema") is None
        data = read_fixture_request(messages)
        self.calls.append((interface, data))
        if interface == "PLAN":
            return fixture_response(smoke_plan().model_dump())
        if interface == "UPDATE":
            facts = []
            for raw in data["window_sources"]:
                if raw["kind"] == "heading":
                    continue
                qid = "Q1" if "teacher" in raw["text"] else "Q2" if "mother" in raw["text"] else "Q3"
                text = raw["text"].replace("Suzhou", "Shanghai") if self.corrupt_extraction else raw["text"]
                facts.append({"query_ids": [qid], "source_refs": [raw["source_ref"]], "text": text})
            return fixture_response({"facts": facts, "hints": []})
        if interface == "RECALL":
            assert "raw_evidence" not in data
            return fixture_response({"selected_fact_ids": data["selectable_fact_ids"]})
        if interface == "MEMORY":
            operations = []
            nodes = {n["fact_id"]: n for n in data["working_memory"]["navigation"]["facts"]}
            source = {r["source_ref"]: r["text"] for r in data["working_memory"]["raw_evidence"]}
            support = {}
            for pending in data["pending_uses"]:
                fid, qid = pending["fact_id"], pending["query_id"]
                fact = nodes[fid]
                assert all(ref in source for ref in fact["source_refs"])
                operation = {"op": "ASSESS", "query_id": qid, "fact_id": fid, "verdict": "ACCEPT",
                             "checked_refs": fact["source_refs"], "reason_code": "RAW_SUPPORTED_AND_APPLICABLE"}
                if "Shanghai" in fact["text"] and any("Suzhou" in source[r] for r in fact["source_refs"]):
                    alias = f"N{len(operations) + 1}"
                    operation["correction"] = {"local_id": alias, "text": fact["text"].replace("Shanghai", "Suzhou"),
                                                "source_refs": fact["source_refs"]}
                    support.setdefault(qid, []).append(alias)
                else:
                    support.setdefault(qid, []).append(fid)
                operations.append(operation)
            for qid, fids in support.items():
                if qid in data["bindable_query_ids"]:
                    operations.append({"op": "BIND", "query_id": qid, "value": {"Q1": "Alice", "Q2": "Mary", "Q3": "Suzhou"}[qid],
                                       "kind": "DIRECT", "support_fact_ids": fids})
            return fixture_response({"operations": operations})
        if interface == "ANSWER":
            raw = data["working_memory"]["raw_evidence"]
            value = "Suzhou" if any("Suzhou" in r["text"] for r in raw) else None
            if data["answer_contract"] == "boxed":
                return "\\boxed{" + (value or "UNKNOWN") + "}"
            if data["answer_contract"] == "text":
                return value or "UNKNOWN"
            return json.dumps({"answer": value, "answer_type": "LOCATION", "source_refs": [r["source_ref"] for r in raw]})
        raise AssertionError(interface)


async def run_smoke(*, store=None, client=None, config=None):
    sample = canonicalize_record({"id": "smoke", "question": "Where was Cindy's teacher's mother born?",
        "answer": "Suzhou", "context": [
            ["Cindy", ["Cindy's teacher is Alice."]],
            ["Alice", ["Alice's mother is Mary."]],
            ["Mary", ["Mary was born in Suzhou."]],
        ]})
    manifest = build_manifest(sample, order="reverse")
    store = store or SQLiteEventStore()
    client = client or ScriptedSmokeClient(corrupt_extraction=True)
    config = config or RunnerConfig(protocol_version="v5.2", plan_format="subqueries", chunk_size=28)
    return await V5Runner(client, config=config).run(run_id="v52-smoke", sample=sample, manifest=manifest, store=store)
