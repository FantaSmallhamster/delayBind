# V08 relation guards: Qwen3.5-9B, 98/128

This snapshot is the code evaluated on 2026-10-13 (Asia/Shanghai) after the three relation-alignment changes. It is distinct from the historical V08 run that also scored 98/128. Its parent is historical V08 commit `f6074ba88762ac14f4a9f7da011c2a11f23989fc`; all 36 core source files and the configuration match the evaluated snapshot byte for byte.

## Changes

1. Retain atomic fact splitting in UPDATE; add concrete subject, directed relation, identity qualifiers, and same-relation positive/negative examples. Downstream facts may remain candidates before the upstream subject is known.
2. In MEMORY_GROUNDED, check the rendered subject → relation → proposed value against the supplied source excerpts. Distinguish parent from grandparent and partial premises from answer evidence. This is a model prompt constraint, not a deterministic semantic proof engine.
3. Normalize BIND/REBIND proposals using the immutable call-start query evidence contract. Reject conflicting values together; retain existing source, support, dependency and version gates. Existing upstream correction invalidates stale descendants.

Only `delaybind_core/agent_prompts.py` and `delaybind_core/subquery_runner.py` differ from historical V08. JSON response schemas and ANSWER/PLAN/RECALL prompts are unchanged. There is no raw archive rescue or new evidence retrieval.

## Fixed old dataset results

| Condition | Correct | EM | F1 |
|---|---:|---:|---:|
| This snapshot | 98/128 | 76.56% | 0.7847 |
| Previous atomic-fact 9B run | 97/128 | 75.78% | 0.7835 |
| Historical V08 9B | 98/128 | 76.56% | 0.7805 |
| Relaxed Direct 9B | 99/128 | 77.34% | 0.8190 |

| Type | Previous 97 | This snapshot |
|---|---:|---:|
| compositional | 32/53 | 27/53 |
| inference | 9/16 | 14/16 |
| bridge_comparison | 21/24 | 22/24 |
| comparison | 35/35 | 35/35 |

This run gained 7 questions and lost 6 against the previous 97 run. All 128 questions ended with status OK. There were 956 completed model calls and 16 retried timeout attempts. Inference recovered, but compositional accuracy decreased; the run does not establish simultaneous improvement of both types.

Five of the six previous inference regressions recovered. Three still had an unbound second hop; one retained a wrong second-hop binding and was corrected by final ANSWER. The remaining recovered case had correct binding values but incomplete source proof. Answer EM therefore does not imply a correct, fully grounded binding chain. See `reviewed-cases.json` for 13 trajectory reviews.

Each condition has one run. The three changes were tested together, and PLAN was regenerated; 89/128 saved plans were identical to the previous run. These results do not isolate each change's causal effect or establish a stable gain.

## Reproduction and verification

Use Python >=3.11 and install the project with test/tokenizer dependencies. The exact config is `configs/ultimate/v08_qwen35_9b_relation_guards_old128_20261013_r1.json`. It requires the fixed old 128-question file and Qwen3.5-9B tokenizer specified there; verify their SHA-256 values against `source-manifest.json`. Supply the API key through `MODEL_API_KEY`.

```sh
python -m pytest tests/test_v04_grounded_memory.py tests/test_v06b_memory_json.py tests/test_source_archive_boundaries.py tests/test_binding_action_corrections.py -q
python scripts/ultimate/run_experiment.py --config configs/ultimate/v08_qwen35_9b_relation_guards_old128_20261013_r1.json --expected-samples 128
```

The four listed test files passed all 26 tests in the publication checkout. Tests use synthetic entities and make no model API calls. Existing results were independently rescored: all four conditions, 512 rows, matched the stored EM/F1; source/config/data/tokenizer hashes and API model/settings were checked.

Settings: Qwen/Qwen3.5-9B on SiliconFlow; 128 questions × 50 documents; temperature 0, top_p 0.95, seed 4, thinking disabled, maximum output 4096 tokens, concurrency 8, window 5000 tokens, memory and source-memory budgets 8192 tokens.

## Artifacts

- `comparison.html`: standalone Chinese score/type comparison and searchable 128-question paired table. Download and open locally to view.
- `summary.json`: aggregate scores, paired changes, pipeline diagnostics and limitations.
- `predictions.jsonl`: original per-question answers and scores, with local filesystem paths and raw API payloads omitted.
- `source-manifest.json`: exact source/config/input fingerprints and ordered sample IDs.
- `reviewed-cases.json`: source-based review of 13 representative trajectories.

The complete original trajectories and API call store remain in the local experiment archive. The original results SHA-256 is recorded for traceability. The historical runs were not modified or rerun for this publication.
