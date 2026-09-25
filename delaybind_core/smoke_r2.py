"""Offline scripted text-wire fixtures. Never used by production inference."""
import re
from .data import canonicalize_record, build_manifest
from .runner import RunnerConfig, V5Runner
from .storage import SQLiteEventStore
from .fixture_wire_r2 import smoke_plan, fixture_response, read_fixture_raw
from .text_protocol_v52 import fields, refs
from .text_views_v52 import escaped


def read_request(messages):
    content = messages[-1]["content"]
    if "Input data:\n" not in content:
        return {"plan": True}
    body = content.split("Input data:\n", 1)[1]
    if "Original request:\n" in body:
        body = body.split("Original request:\n", 1)[1]

    def section(label):
        match = re.search(r"(?:^|\n\n)" + re.escape(label) + r":\n+(.*?)(?=\n\n[A-Z][A-Za-z /()-]+:\n|\Z)", body, re.S)
        return match[1] if match else "NONE"

    fact_only = (("Evidence mode:" in content and "FACT_ONLY" in content)
                 or "MEMORY，HIGH，事实模式" in content
                 or "constrained fact-extraction interface" in messages[0]["content"])

    def facts(text):
        parsed = [fields(line) for line in text.splitlines()]
        output = []
        for f in parsed:
            if not f or not f[0].startswith("F"):
                continue
            if len(f) == 3:
                output.append(dict(fact_id=f[0], source_refs=refs(f[1]), text=f[2]))
            elif fact_only and len(f) == 2:
                output.append(dict(fact_id=f[0], source_refs=[], text=f[1]))
        return output

    query = section("Current query") if section("Current query") != "NONE" else section("Query")
    match = re.match(r"(Q\d+)(?: \[(\w+)\])? (?:\| )?([^\n]+)", query)
    wm = section("Eligible facts")
    if wm == "NONE":
        wm = section("Working memory").removeprefix("Fact navigation:\n")
    staged = []
    for line in section("Staged reviews").splitlines():
        f = re.search(r"\bfact_id=(F[0-9a-f]+); verdict=(\w+)", line)
        if f:
            corrected = re.search(r"corrected_fact_id=(F[0-9a-f]+)", line)
            staged.append(dict(fact_id=corrected[1] if corrected else f[1], verdict=f[2]))
    window_text = section("Current window")
    window = ([dict(source_ref="", kind="text", complete=True, text=window_text)] if fact_only and window_text != "NONE"
              else read_fixture_raw(window_text))
    return dict(body=body, question=section("Question"), query_id=match[1] if match else None,
        rendered_query=match[3] if match else "", statuses=dict(re.findall(r"(Q\d+) \[(ACTIVE|DORMANT|RESOLVED)\]", section("Plan"))),
        window=window, raw=[] if fact_only else read_fixture_raw(section("Original evidence")),
        facts=facts(wm), candidates=facts(section("Candidates")), selectable=refs(section("Selectable IDs")),
        mode=section("Mode"), phase=section("Phase"), required=refs(section("Required reviews")), staged=staged,
        answer_format=section("Answer format"), fact_only=fact_only)


class ScriptedR2Client:
    """Fixed semantic choices for synthetic materials, not an accuracy evaluator."""
    def __init__(self):
        self.calls = []

    async def complete(self, *, interface, messages, **kwargs):
        assert kwargs.get("response_schema") is None
        data = read_request(messages)
        self.calls.append((interface, data, kwargs.get("local_metadata")))
        if interface == "PLAN":
            return fixture_response(smoke_plan().model_dump())
        if interface in {"UPDATE", "UPDATE_REPAIR"}:
            lines = []
            for source in data["window"]:
                if source["kind"] == "heading":
                    continue
                pieces = re.split(r"(?<=[.!?])|\n", source["text"].strip()) if data["fact_only"] else source["text"].strip().split("\n")
                for text in pieces:
                    text = text.strip()
                    if (not text or text.startswith("Document ")
                            or not any(word in text for word in ("teacher", "mother", "born", "Other Cindy", "Correction:", "Disputed:"))):
                        continue
                    qid = "Q1" if "teacher" in text or "Other Cindy" in text else "Q2" if "mother" in text else "Q3"
                    lines.append(f"{qid} | {escaped(text)}" if data["fact_only"] else
                                 f"{qid} | {source['source_ref']} | {escaped(text)}")
            return "\n".join(lines) or "NONE"
        if interface == "RECALL":
            return "SELECT " + ",".join(data["selectable"]) if data["selectable"] else "NONE"
        if interface in {"MEMORY", "MEMORY_REPAIR"}:
            mode, qid = data["mode"], data["query_id"]
            rows = []
            facts = {f["fact_id"]: f for f in data["facts"]}
            texts = " ".join(f["text"] for f in facts.values()) if data["fact_only"] else " ".join(r["text"] for r in data["raw"])
            conflict = qid == "Q1" and "Disputed" in texts
            replace = qid == "Q1" and "Correction:" in texts
            if data["fact_only"]:
                if conflict:
                    return "UNBOUND"
                matches = []
                for fid, fact in facts.items():
                    text = fact["text"]
                    if "Other Cindy" in text:
                        continue
                    if qid == "Q1" and (("Correction:" in text) == replace) and "teacher" in text:
                        matches.append(fid)
                    elif qid == "Q2" and "mother" in text and (("Bob" in data["rendered_query"] and "Bob" in text)
                                                                  or ("Bob" not in data["rendered_query"] and "Alice" in text)):
                        matches.append(fid)
                    elif qid == "Q3" and "born" in text and (("June" in data["rendered_query"] and "June" in text)
                                                                or ("June" not in data["rendered_query"] and "Mary" in text)):
                        matches.append(fid)
                if not matches:
                    return "UNBOUND"
                value = ("Bob" if replace else "Alice") if qid == "Q1" else (
                    "June" if "Bob" in data["rendered_query"] else "Mary") if qid == "Q2" else (
                    "Beijing" if "June" in data["rendered_query"] else "Suzhou")
                return f"BOUND | {value} | {','.join(matches)}"
            for fid in data["required"]:
                f = facts[fid]
                reject = ("Other Cindy" in f["text"] or (replace and "Correction:" not in f["text"]) or
                          (qid == "Q2" and "Bob" in data["rendered_query"] and "Alice" in f["text"]) or
                          (qid == "Q3" and "June" in data["rendered_query"] and "Mary" in f["text"]))
                verdict = "CONFLICT" if conflict else "REJECT" if reject else "ACCEPT"
                rows.append(f"REVIEW | {fid} | {verdict} | {','.join(f['source_refs'])}")
            if data["phase"] == "REVIEW":
                return "\n".join(rows)
            else:
                accepted = [x["fact_id"] for x in data["staged"] if x["verdict"] == "ACCEPT"]
                if not accepted or conflict:
                    rows.append("UNBOUND | " + (",".join(r["source_ref"] for r in data["raw"]) or "NONE"))
                else:
                    support = accepted
                    if qid == "Q1":
                        support = [f for f in accepted if ("Correction:" in facts[f]["text"]) == replace]
                        value = "Bob" if replace else "Alice"
                    elif qid == "Q2":
                        value = "June" if "Bob" in data["rendered_query"] else "Mary"
                    else:
                        value = "Beijing" if "June" in data["rendered_query"] else "Suzhou"
                    rows.append(f"BOUND | {value} | {','.join(support)} | NONE")
            return "\n".join(rows)
        if interface == "ANSWER":
            texts = (" ".join(x["text"] for x in data["facts"]) if data["fact_only"]
                     else " ".join(x["text"] for x in data["raw"]))
            value = "UNKNOWN" if "Disputed" in texts else "Beijing" if "June was born" in texts else "Suzhou" if "Mary was born" in texts else "UNKNOWN"
            return "\\boxed{" + value + "}" if data["answer_format"] == "boxed" else value
        raise AssertionError(interface)


async def run_smoke(*, outcome="keep", config=None, client=None, store=None):
    tail = {"keep": "Other Cindy likes Alice.", "replace": "Correction: Cindy's teacher is Bob, not Alice.",
            "unbound": "Disputed: Cindy's teacher is Bob; neither account can be preferred."}[outcome]
    docs = [["Mary", ["Mary was born in Suzhou."]], ["Alice", ["Alice's mother is Mary."]],
            ["Cindy", ["Cindy's teacher is Alice."]], ["Review", [tail]]]
    if outcome == "replace":
        docs += [["June", ["June was born in Beijing."]], ["Bob", ["Bob's mother is June."]]]
    sample = canonicalize_record(dict(id="r2-smoke", question="Where was Cindy's teacher's mother born?", context=docs))
    return await V5Runner(client or ScriptedR2Client(), config=config or RunnerConfig(protocol_version="v5.2-r2", chunk_size=32)).run(
        run_id="r2-smoke-" + outcome, sample=sample, manifest=build_manifest(sample), store=store or SQLiteEventStore())
