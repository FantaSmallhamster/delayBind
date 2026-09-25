from copy import deepcopy

import pytest

from delaybind_core.agent_prompts_v52 import messages as v52_messages
from delaybind_core.prompts_r2 import messages as r2_messages
from delaybind_core.archive import SentenceArchive
from delaybind_core.cursor_v52 import TextReadCursor
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.text_views_v52 import raw_view, queries_view
from delaybind_core.token_budget import TokenCounter
from delaybind_core.fixture_wire_r2 import read_fixture_raw
from test_v52_sources import CharacterTokenizer


def test_compact_sources_preserve_raw_characters_and_internal_metadata():
    sources = [dict(source_ref=ref, text=text, kind=kind, complete=complete, text_sha256="internal")
               for ref, text, kind, complete in [
                   ("D0:H0", "  Title\t", "heading", True),
                   ("D0:S0", "First line.\n  Second line.\n", "sentence", True),
                   ("D0:S1:P1", "unfinished ", "fragment", False),
                   ("D1@C0", "chunk\n\n", "chunk", True)]]
    before = deepcopy(sources)
    rendered = raw_view(sources)
    assert rendered == "".join(f"[{r['source_ref']}] {r['text']}\n" for r in sources)
    assert sources == before
    assert all(field not in rendered for field in ("kind=", "complete=", "chars=", "text_sha256"))
    assert read_fixture_raw(rendered) == [{k: v for k, v in r.items() if k != "text_sha256"} for r in sources]
    assert raw_view([]) == "NONE"


@pytest.mark.parametrize("messages", [v52_messages, r2_messages])
@pytest.mark.parametrize("interface", ["UPDATE", "UPDATE_REPAIR", "ANSWER"])
def test_model_inputs_use_compact_sources_everywhere(messages, interface):
    evidence = dict(source_ref="D0:S0:P1", text="  verbatim\ntext ", kind="fragment", complete=False)
    payload = dict(question="q", query_graph={"queries": []}, working_memory={"raw_evidence": [evidence]},
                   window_sources=[{**evidence, "source_ref": "D1@C0", "kind": "chunk", "complete": True}],
                   visible_sources=["D0:S0:P1", "D1@C0"], answer_contract="text")
    payload.update(repair_targets=[], rejected_items=[], retained_items=[])
    before = deepcopy(payload)
    rendered = "\n".join(m["content"] for m in messages(interface, payload))
    assert "[D0:S0:P1]   verbatim\ntext \n" in rendered
    if interface in {"UPDATE", "UPDATE_REPAIR"}:
        assert "[D1@C0]   verbatim\ntext \n" in rendered
    assert all(field not in rendered for field in ("kind=", "complete=", "chars="))
    assert ":P 后缀为不完整片段" in rendered
    assert "Citable" not in rendered
    assert "包括工作记忆中的原文；不能仅凭事实摘要引用来源" in rendered
    assert payload == before


@pytest.mark.parametrize("rendered_query", [None, "Where was ?director born?", "Where was Alice born?"])
def test_query_template_shown_only_when_different(rendered_query):
    query = dict(id="Q2", status="ACTIVE", template="Where was ?director born?",
                 output="?birthplace", inputs={"?director": "Q1"})
    if rendered_query is not None:
        query["rendered_query"] = rendered_query
    before = deepcopy(query)
    view = queries_view({"queries": [query]})
    assert view.count(query["template"]) == 1
    assert ("  query:" in view) == (rendered_query == "Where was Alice born?")
    assert "depends_on: Q1" in view
    assert query == before


@pytest.mark.parametrize("messages", [v52_messages, r2_messages])
def test_memory_and_repair_keep_raw_but_not_citable_list(messages):
    payload = dict(question="q", context_id="c", query_graph={"queries": []},
        query_instance=dict(id="Q1", template="Who?", output="?who"),
        working_memory={"raw_evidence": [dict(source_ref="D0:S0", text="Alice.")]},
        pending_uses=[], input_signatures={}, allowed_fact_ids=[], allowed_source_refs=["D0:S0"],
        visible_source_refs=["D0:S0"], bindable_query_ids=[], review_barriers={},
        context_request_limits={}, scope_closed=False, allowed_mode="BIND", phase="REVIEW",
        required_reviews=[], allowed_review_ids=[], staged_reviews=[], barriers={}, context_limits={})
    before = deepcopy(payload)
    repair = dict(context_id="c", validation_errors="test", rejected_response="bad",
                  original_memory_request=payload)
    for interface, data in [("MEMORY", payload), ("MEMORY_REPAIR", repair)]:
        rendered = "\n".join(m["content"] for m in messages(interface, data))
        assert "Citable" not in rendered
        assert "[D0:S0] Alice.\n" in rendered
    assert payload == before


def test_update_without_printed_whitelist_still_rejects_summary_only_and_unshown_sources():
    from test_r2_core import fixture
    from delaybind_core.context_r2 import build_update_context
    from delaybind_core.protocol_r2 import parse_update

    runtime = fixture()
    payload = build_update_context(runtime, "q", ["D0:S0"])
    # D0:S1 has been read but is not shown as raw in this request.
    assert runtime.archive.fetch_sentence("D0:S1")
    payload["working_memory"]["navigation"]["facts"].append(
        dict(fact_id="Fsummary", source_refs=["D0:S1"], text="Summary only",
             query_id="Q1", use_status="PENDING", input_signature="s"))
    before = deepcopy(payload)
    assert "Citable" not in r2_messages("UPDATE", payload)[1]["content"]
    response = ("Q1 | D0:S0 | Visible source\n"
                "Q1 | D0:S1 | Summary only\n"
                "Q1 | D99:S0 | Invented source")
    valid, errors = parse_update(response, payload)
    assert len(valid.facts) == 1 and len(errors) == 2
    assert all("SOURCE_NOT_VISIBLE" in e["error"] for e in errors)
    assert payload == before


def test_rendered_window_matches_cursor_budget_even_for_fragments():
    archive = SentenceArchive(SQLiteEventStore(), "compact")
    counter = TokenCounter(CharacterTokenizer())
    cursor = TextReadCursor("Mary " + "really " * 20 + "was born in Suzhou.", archive,
                            sample_id="s", counter=counter, window_mode="fragment")
    while not cursor.exhausted:
        window = cursor.next_anchored_window(65)
        sources = [archive.fetch_sentence(ref).model_dump(mode="json") for ref in window.source_refs]
        assert counter.count(raw_view(sources)) <= 65
        assert all(source["complete"] is False for source in sources)
