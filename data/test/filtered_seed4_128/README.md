# Frozen 2Wiki Filtered Benchmark

This directory contains the frozen 128-question benchmark selected from the
three completed no-context filtering batches.

## Final type quotas

| Type | Questions |
| --- | ---: |
| compositional | 53 |
| inference | 16 |
| comparison | 35 |
| bridge_comparison | 24 |
| total | 128 |

The original proportional target was 53/16/31/28. The four unavailable
bridge_comparison slots were reassigned to comparison as requested.

Selection uses one `Random(4)` instance and samples the combined retained pool
in this fixed order: compositional, inference, comparison, bridge_comparison.
All questions passed the filter rule of four valid no-context answers with
official EM equal to zero.

## Files

- `eval_2wikimultihopqa_50.json`: runnable benchmark with 50 documents per question.
- `selected_official.json`: aligned original official 2Wiki records.
- `selected_source.jsonl`: aligned FlashRAG-format source records.
- `questions.csv`: compact question list for spreadsheet inspection.
- `frozen_manifest.json`: exact IDs, source hashes, output hashes and selection rules.

The benchmark SHA-256 is
`c8bdf141c889766ef7a1aa0e4e8421ddcabe0c1995bf592ff2608b7a6484eeff`.

## Run

Validate the frozen input and local tokenizer without making API calls:

```bash
python scripts/run_v51_smoke.py \
  --config configs/v51_2wiki50_filtered_seed4_128.json \
  --prepare-only
```

Run all 128 questions after setting `MODEL_API_KEY` in the environment or an
env file:

```bash
python scripts/run_v51_smoke.py \
  --config configs/v51_2wiki50_filtered_seed4_128.json \
  --env-file /path/to/.env.local
```
