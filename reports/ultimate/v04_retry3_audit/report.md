# V04 retry3 full 128 evaluation

V04 `MEMORY_GROUNDED` completed all 128 fixed seed-4 2Wiki questions with Qwen/Qwen3.5-9B, seed 4, concurrency 8.

- Correct: **92/128 (71.875%)**
- Answer token F1: **0.740972**
- Status: 127 `OK`, 1 `ERROR`
- Error: `dev_4060`, final response missing `\\boxed{...}`

Compared with the frozen C run (92/128, F1 0.735454): 7 gains and 7 losses, so exact accuracy is unchanged and token F1 increases. The protected comparison subset is 34/35 in this run, so the strict V04 gate remains BLOCKED. The loss IDs are recorded in `v04_joint_source_memory_gate.json` and `v04_paired_diff.csv`.

V04 used 983 logical model calls, 989 network requests, 0 cache hits, 6 retries, 2,227,096 input tokens and 308,698 output tokens. C used 1,348 logical calls, 1,409 network requests, 8 cache hits, 69 retries, 2,791,762 input tokens and 301,830 output tokens. V04 is below the 105% combined token budget and uses fewer network requests.

This is one valid V04 run. The plan requires three independent candidate runs and matched parent runs before promotion; those runs have not been performed. The C parent remains accepted.
