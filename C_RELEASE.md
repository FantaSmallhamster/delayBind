# DelayBind C source-first experiment

This release records the C-stage implementation and its frozen 128-question evaluation. C moves `MEMORY_VERIFY` before `MEMORY`: the checker labels each saved fact against its original source as `SUPPORTED`, `SOURCE_DIFF`, or `UNRESOLVED`, and `MEMORY` decides whether to bind using the eligible source-backed facts. C does not call the proposal-level postcheck in its active path.

**Result: BLOCKED.** C scored 92/128 (answer token F1 0.7355), below the accepted B comparison at 94/128 (F1 0.7625). There were 4 gains and 6 losses on paired question IDs. C is an experiment, not the new stable default. See [the stage report](reports/source_first/c_stage_report.md), [gate](reports/source_first/c_gate.json), and [paired results](reports/source_first/paired_diff.csv).

## Contents

- `delaybind_core/`: the source used for the C run, including source precheck and the B comparison mode.
- `configs/source_first/`: frozen A/B/C experiment configurations; the C full run uses `c_source_verify_full128.json`.
- `data/test/filtered_seed4_128/` and `models/Qwen3.5-9B-tokenizer/tokenizer.json`: frozen evaluation inputs.
- `tests/`: portable C-path protocol and integration tests (51 cases passed). The full development worktree also passed 106 cases, including historical stage tests that are not bundled here.
- `results/source_first/b-memory-raw-c8/` and `results/source_first/c-source-verify-c8-r2/`: complete per-question results and model trajectories, manifests, effective configs, and run logs. SQLite caches and duplicate completion journals are omitted from the portable package.
- `reports/source_first/`: frozen sample manifest, smoke gate, full gate, paired diff, and loss analysis.
- `scripts/staged_refactor/run_experiment.py` and `compare_source_first_c.py`: run and audit entry points.

The portable package omits historical rescue, low-level verify, and raw-fallback experiment configs, replay snapshots, and scripts. The current source retains guards that reject obsolete options, plus legacy graph APIs from the upstream repository; neither was active in the C run. The exact source hashes for the evaluated code are in the C run's `code_manifest.json`.

## Verify

```bash
python -m unittest \
  tests.test_prebinding_source_verify \
  tests.test_source_first_integration \
  tests.test_memory_recheck_protocol \
  tests.test_source_archive_boundaries \
  tests.test_answer_normalization \
  tests.test_stage5_rememr1_answer_prompt \
  tests.test_step1b_verify -q
python scripts/staged_refactor/compare_source_first_c.py \
  --baseline results/source_first/b-memory-raw-c8 \
  --candidate results/source_first/c-source-verify-c8-r2 \
  --report-dir reports/source_first
```

The comparison exits with code 2 because the C accuracy gate is blocked. To rerun the API evaluation, set `MODEL_API_KEY` in the process environment and run:

```bash
python scripts/staged_refactor/run_experiment.py \
  --config configs/source_first/c_source_verify_full128.json \
  --expected-samples 128
```

The run wrapper requires an empty output directory; choose a new run ID and directory for another attempt. The stored results use the project's `answer_exact` normalization and include every recorded error in the 128-question denominator.
