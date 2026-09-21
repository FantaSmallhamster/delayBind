"""Fresh eight-question live smoke test, with experiment-only P17-full BIND.

Unlike evaluate_r2_memory_chain, no PLAN or UPDATE output is replayed. All
interfaces use the live client. The production prompt dispatch stays unchanged.
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delaybind_core import prompts_r2 as prompts, subquery_runner_r2 as loop
from delaybind_core.cli import _load_config, _load_env_file, _api_config, _load_tokenizer
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness

BASE_CONFIG = ROOT / "configs/v52_r2_smoke8_text7_fact_only.json"
DEFAULT_OUTPUT = ROOT / "results/v52-r2-smoke8-p17-full-live-20260921-run1"
VERSION = "experiment-P17-full-live-smoke8"
SAMPLE_IDS = {"dev_5343", "dev_4784", "dev_11393", "dev_954", "dev_91", "dev_10616", "dev_4961", "dev_7920"}


def is_bind(interface, payload):
    original = payload.get("original_memory_request", payload)
    return interface in {"MEMORY", "MEMORY_REPAIR"} and original.get("fact_only") and original.get("allowed_mode") == "BIND"


def p17_messages(interface, payload):
    messages = prompts.messages(interface, payload)
    if is_bind(interface, payload):
        assert messages[1]["content"].count(prompts.MEMORY_FACT_ONLY_BIND) == 1
        messages[1]["content"] = messages[1]["content"].replace(prompts.MEMORY_FACT_ONLY_BIND, prompts.MEMORY_BIND_P17, 1)
    return messages


def p17_version(interface, payload):
    return VERSION if is_bind(interface, payload) else prompts.prompt_version_for(interface, payload)


class ProgressHarness(ExperimentHarness):
    async def _run_condition(self, sample, method, order, oracle_plans):
        print("START " + sample.sample_id + " " + sample.question, flush=True)
        result = await super()._run_condition(sample, method, order, oracle_plans)
        # A completed question remains inspectable if a later call is paused.
        with (self.output_dir / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({key: result.get(key) for key in (
            "sample_id", "runtime_status", "status", "prediction", "answer_exact", "model_calls", "error")}, ensure_ascii=False), flush=True)
        return result


async def run(output, env_file):
    if output.exists():
        raise ValueError("Use a new output directory; never overwrite or replay an earlier smoke test")
    # Historical P17-full presupposes SINGLE/SET and the old runtime. Refuse
    # before loading credentials or sending PLAN, rather than mislabel a new
    # member-graph experiment as an old-prompt-only comparison.
    from scripts.evaluate_r2_memory_ab import assert_frozen_contracts
    assert_frozen_contracts()
    _load_env_file(env_file)
    raw = _load_config(BASE_CONFIG)
    raw.update(output_dir=str(output), experiment_id=output.name, resume=False, retry_errors=False)
    raw["input"] = str(ROOT / raw["input"])
    raw["tokenizer_path"] = str(ROOT / raw["tokenizer_path"])
    cfg = ExperimentConfig.from_mapping(raw)
    assert not cfg.runner.sentence_splitting and cfg.runner.chunk_size == 5000
    api = _api_config(raw, default_seed=cfg.seed)
    harness = ProgressHarness(cfg, api_config=api, tokenizer=_load_tokenizer(cfg.tokenizer_path))
    samples = harness._samples()
    assert len(samples) == 8 and {s.sample_id for s in samples} == SAMPLE_IDS
    unchanged_names = ("MEMORY_FACT_ONLY", "MEMORY_FACT_ONLY_BIND", "SYSTEM_FACT_ONLY_BIND", "UPDATE_FACT_ONLY", "PROMPT_VERSION")
    before = {name: hashlib.sha256(getattr(prompts, name).encode()).hexdigest() for name in unchanged_names}
    output.mkdir(parents=True)
    with (output / "experiment_variant.json").open("x", encoding="utf-8") as stream:
        json.dump(dict(
            variant=VERSION, production_default_unchanged=True, sample_ids=[s.sample_id for s in samples],
            all_interfaces_live=True, frozen_plan_or_update_outputs=False, old_runtime_or_response_cache_reused=False,
            changed_interface="FACT_ONLY MEMORY BIND and its protocol repair only",
            p17_instruction_sha256=hashlib.sha256(prompts.MEMORY_BIND_P17.encode()).hexdigest(),
            unchanged_prompt_sha256=before, input_sha256=hashlib.sha256(Path(raw["input"]).read_bytes()).hexdigest(),
            api_parameters={k: v for k, v in vars(api).items() if k != "api_key"},
            source_config=str(BASE_CONFIG), runner=raw["runner"],
        ), stream, ensure_ascii=False, indent=2)
    # One variant for this entire standalone process; no cross-version races.
    # Fresh PLAN/UPDATE/RECALL clients remain exactly the standard live clients.
    with patch.object(loop, "messages", p17_messages), patch.object(loop, "prompt_version_for", p17_version), patch.object(loop, "MEMORY_BIND_PROMPT_VERSION", VERSION):
        summary = await harness.run()
    assert before == {name: hashlib.sha256(getattr(prompts, name).encode()).hexdigest() for name in unchanged_names}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.env_file))
