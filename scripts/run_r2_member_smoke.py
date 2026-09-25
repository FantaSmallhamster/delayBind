"""Fresh live smoke test of the production member-graph runtime, without overrides."""
import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from delaybind_core import prompts_r2
from delaybind_core.cli import _api_config, _load_config, _load_env_file, _load_tokenizer
from delaybind_core.evaluation import ExperimentConfig, ExperimentHarness

SAMPLE_IDS = ["dev_5343", "dev_4784", "dev_11393", "dev_954", "dev_91", "dev_10616", "dev_4961", "dev_7920"]


class ProgressHarness(ExperimentHarness):
    async def _run_condition(self, sample, method, order):
        print(f"START {sample.sample_id} {sample.question}", flush=True)
        result = await super()._run_condition(sample, method, order)
        with (self.output_dir / "progress.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
        print(json.dumps({key: result.get(key) for key in (
            "sample_id", "runtime_status", "status", "prediction", "answer_exact", "model_calls", "error"
        )}, ensure_ascii=False), flush=True)
        return result


async def run(config_path, output):
    raw = _load_config(config_path)
    output = (output or ROOT / raw["output_dir"]).resolve()
    if output.exists():
        raise ValueError("Use a fresh output directory; existing runs are never overwritten or resumed")
    _load_env_file(ROOT / ".env.local")
    raw.update(output_dir=str(output), experiment_id=output.name, resume=False, retry_errors=False)
    for name in ("input", "tokenizer_path"):
        raw[name] = str(ROOT / raw[name])
    config = ExperimentConfig.from_mapping(raw)
    api = _api_config(raw, default_seed=config.seed)
    harness = ProgressHarness(config, api_config=api, tokenizer=_load_tokenizer(config.tokenizer_path))
    samples = harness._samples()
    if [sample.sample_id for sample in samples] != SAMPLE_IDS:
        raise ValueError("Smoke sample IDs/order differ from the agreed eight questions")
    sources = sorted((ROOT / "delaybind_core").glob("*.py")) + [Path(__file__).resolve()]
    audit = dict(
        started_at=datetime.now(timezone.utc).isoformat(),
        branch=subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
        head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        prompt_version=prompts_r2.MEMBER_PROMPT_VERSION,
        all_interfaces_live=True, prompt_overrides=False, replayed_outputs=False,
        previous_cache_reused=False, sample_ids=SAMPLE_IDS,
        source_config=str(config_path),
        source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        input_sha256=hashlib.sha256(Path(raw["input"]).read_bytes()).hexdigest(),
        api_parameters={key: value for key, value in vars(api).items() if key != "api_key"},
        runner=raw["runner"],
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "run_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(await harness.run(), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/v52_r2_smoke8_member_graph.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.config.resolve(), args.output))
