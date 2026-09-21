"""Named UPDATE candidates remain reproducible but inactive until selected."""

from pathlib import Path

from delaybind_core import prompts_r2
from delaybind_core.update_prompt_styles import MEM0_STYLE, MEM0_STYLE_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_mem0_style_is_the_exact_tested_draft_and_is_exposed_by_prompts_module():
    frozen = (ROOT / "experiments/update_p1p5_draft/prompt.txt").read_text(encoding="utf-8").rstrip()
    assert MEM0_STYLE_VERSION == "mem0_style"
    assert MEM0_STYLE == frozen
    assert prompts_r2.UPDATE_FACT_ONLY_MEM0_STYLE == frozen
    assert prompts_r2.MEM0_STYLE_VERSION == "mem0_style"


def test_mem0_style_is_retained_but_not_active_production_update():
    assert prompts_r2.UPDATE_FACT_ONLY != MEM0_STYLE
