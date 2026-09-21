"""The live smoke override must preserve inputs and every other interface."""
from delaybind_core import prompts_r2 as p
from scripts.run_r2_p17_smoke import p17_messages, p17_version, VERSION
from test_r2_memory_ab import sample_case


def test_smoke_bind_uses_p17_with_identical_full_input():
    payload = sample_case()["payload"]
    original = p.messages("MEMORY", payload)
    actual = p17_messages("MEMORY", payload)
    assert actual[0] == original[0]
    assert actual[1]["content"] == original[1]["content"].replace(p.MEMORY_FACT_ONLY_BIND, p.MEMORY_BIND_P17, 1)
    assert actual[1]["content"].split("Input data:\n")[1] == original[1]["content"].split("Input data:\n")[1]
    assert p17_version("MEMORY", payload) == VERSION
    assert p.prompt_version_for("MEMORY", payload) == p.MEMORY_BIND_PROMPT_VERSION


def test_smoke_keeps_rebind_and_plan_unchanged_and_repair_consistent():
    payload = sample_case()["payload"]
    rebound = {**payload, "allowed_mode": "REBIND"}
    assert p17_messages("MEMORY", rebound) == p.messages("MEMORY", rebound)
    assert p17_version("MEMORY", rebound) == p.PROMPT_VERSION
    initial = dict(question="Who directs Film Aspen?", validation_errors=None, fact_only=True)
    assert p17_messages("PLAN", initial) == p.messages("PLAN", initial)
    assert p17_version("UPDATE", payload) == p.PROMPT_VERSION
    repair = dict(original_memory_request=payload, validation_errors="INVALID", rejected_response="bad")
    assert p.MEMORY_BIND_P17 in p17_messages("MEMORY_REPAIR", repair)[1]["content"]
    assert p17_version("MEMORY_REPAIR", repair) == VERSION
