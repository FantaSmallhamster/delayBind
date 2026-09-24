"""The fact-only UPDATE hint contract follows the actual R2 configuration."""

from delaybind_core.context_r2 import build_update_context
from delaybind_core.plan_repair_r2 import build_repair_context
from delaybind_core.prompts_r2 import (
    MEMBER_UPDATE_HINT_PROMPT_VERSION,
    MEMBER_UPDATE_PROMPT_VERSION,
    MEMBER_UPDATE_RAW_PROMPT_VERSION,
    messages,
    prompt_version_for,
)
from delaybind_core.protocol_r2 import parse_update
from delaybind_core.runner import RunnerConfig
from delaybind_core.schema_r2 import EvidencePlanR2
from test_r2_core import fixture


def update_payload(*, repair_mode):
    plan = EvidencePlanR2(plan_id="hint", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
        dict(id="Q2", template="Where was ?teacher born?", output="?place",
             inputs={"?teacher": "Q1"}),
    ])
    runtime = fixture(config=RunnerConfig(
        protocol_version="v5.2-r2", sentence_splitting=False,
        plan_repair_mode=repair_mode), plan=plan)
    payload = build_update_context(runtime, "Who teaches Cindy?", ["D0:S0"])
    return runtime, payload


def test_on_hint_exposes_a_legal_hint_line_and_reaches_plan_repair():
    runtime, payload = update_payload(repair_mode="on_hint")
    assert payload["plan_hints_enabled"]
    prompt = messages("UPDATE", payload)[1]["content"]
    assert "PLAN_HINT | source-supported fact and missing evidence need" in prompt
    assert "Use only listed Q IDs for facts; PLAN_HINT is the exception" in prompt
    assert "A missing value for an existing target is not a plan gap" in prompt
    assert "Copy the smallest self-contained source sentence or clause" in prompt
    assert "complete source-supported fact, or the PLAN_HINT exception" in prompt
    assert "Use only the Q1, Q2, ... IDs listed" not in prompt
    assert prompt_version_for("UPDATE", payload) == MEMBER_UPDATE_HINT_PROMPT_VERSION

    response, rejected = parse_update(
        "Q1 | Cindy's teacher is Alice.\n"
        "PLAN_HINT | Alice used the name Alice Vale; an alias evidence need is missing.", payload)
    assert not rejected and len(response.facts) == len(response.hints) == 1
    runtime.ingest(response, payload)
    repair = build_repair_context(runtime, "Who teaches Cindy?")
    assert repair["hint_ids"]


def test_hint_instruction_is_absent_when_plan_repair_is_disabled():
    _, payload = update_payload(repair_mode="disabled")
    assert not payload.get("plan_hints_enabled")
    prompt = messages("UPDATE", payload)[1]["content"]
    assert "PLAN_HINT |" not in prompt
    assert "Copy the smallest self-contained source sentence or clause" in prompt
    assert "A short compound clause is better than dropping a necessary qualifier" in prompt
    assert "Each line must contain one subject, one relation, and one value" not in prompt
    assert "complete atomic fact" not in prompt
    assert prompt_version_for("UPDATE", payload) == MEMBER_UPDATE_PROMPT_VERSION


def test_update_repair_keeps_the_same_hint_setting_and_frozen_scope():
    _, payload = update_payload(repair_mode="on_hint")
    repair = {**payload, "repair_targets": [{"repair_id": "R1", "kind": "PLAN_HINT", "text": "alias"}],
              "rejected_items": [], "retained_items": [], "validation_errors": "bad hint"}
    prompt = messages("UPDATE_REPAIR", repair)[1]["content"]
    assert "Only repair displayed frozen PLAN_HINT targets; do not add a new hint." in prompt
    assert "Copy the smallest self-contained source sentence or clause" in prompt
    assert "Frozen repair targets:" in prompt
    assert prompt_version_for("UPDATE_REPAIR", repair) == MEMBER_UPDATE_HINT_PROMPT_VERSION


def test_raw_member_update_keeps_its_existing_version():
    plan = EvidencePlanR2(plan_id="raw", queries=[
        dict(id="Q1", template="Who teaches Cindy?", output="?teacher"),
    ])
    runtime = fixture(config=RunnerConfig(protocol_version="v5.2-r2", plan_repair_mode="on_hint"), plan=plan)
    payload = build_update_context(runtime, "Who teaches Cindy?", ["D0:S0"])
    assert not payload.get("plan_hints_enabled")
    assert prompt_version_for("UPDATE", payload) == MEMBER_UPDATE_RAW_PROMPT_VERSION
    assert "Copy the smallest self-contained source sentence or clause" not in messages("UPDATE", payload)[1]["content"]
