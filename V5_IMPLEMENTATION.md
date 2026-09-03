# V5 Implementation Status

This checkout is pinned to ReMemR1 commit
`cc514c092ca968a50c52cdcc2e2ba96362fce25a` and keeps the upstream recurrent
baseline intact. The V5 implementation is added independently under
`delaybind_core/`.

## Current CPU-only core

- Pydantic v2 schemas for plans, events, claims, verification and evidence.
- Canonical 2Wiki/FlashRAG record conversion.
- Deterministic sentence-level manifests and a forward-only `ReadCursor`.
- A read-gated `RawArchive` that rejects future source references.
- SQLite runtime/model-call/raw-span storage with idempotent runtime events.
- Delayed binding: `DEFER -> BIND -> exact lookup -> VERIFY -> PROMOTE`.
- Runner 默认让 grounded Claim 先进入 `pending`，统一经过 VERIFY 后再绑定并进入图；
  `verify_committed=false` 可用于低成本消融。
- Pure event-log replay.
- Deterministic `COUNT`/comparison/set/`PROJECT` operators with an auditable trace.
- Logical model-call and window budgets plus per-window SQLite snapshots.
- Operator profiling and one-line CLI utilities.
- Versioned PLAN/UPDATE/VERIFY/ANSWER prompt builders and a training-free V5
  runner over the OpenAI-compatible client.
- Deterministic QueryPlan validation: question anchoring, variable/component
  connectivity, answer targets, operator registry, and one corrective PLAN
  retry with an auditable validation error.
- One-line `run --config` execution that loads `.env.local`, builds/reuses a
  manifest, persists SQLite calls/events, and exports a JSON result.

## Commands

```bash
PYTHONPATH=... python -m delaybind_core profile \
  --input data/2wiki/dev.jsonl --output runs/profile.json

PYTHONPATH=... python -m delaybind_core build-manifest \
  --input data/2wiki/dev.jsonl --sample-index 0 \
  --output manifests/dev_0.json --order reverse --seed 4

PYTHONPATH=... python -m delaybind_core run --config configs/base.json

PYTHONPATH=... python -m delaybind_core experiment \
  --config configs/experiment_smoke.json
```

The `run` command is API-backed and requires `input` plus model credentials.
Credentials are read from `.env.local` (or `MODEL_BASE_URL`, `MODEL_NAME`, and
`MODEL_API_KEY` already exported in the environment). CPU-only commands never
make model calls. Oracle Plan compilation, Direct/V5 batch evaluation, core
metric aggregation, and artifact export are implemented. Open-ended rule
execution and the ReMemR1/verl adapter remain future work.

For a local API configuration, create `.env.local` (it is git-ignored):

```dotenv
MODEL_BASE_URL=https://api.example.com/v1
MODEL_NAME=Qwen/Qwen3.5-9B
MODEL_API_KEY=...
```

The client accepts a host URL, a `/v1` URL, or a full
`/v1/chat/completions` URL and always sends structured requests with thinking
disabled for PLAN/UPDATE/VERIFY by default.
