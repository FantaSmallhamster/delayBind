# VERIFY step 1 from the authoritative V08 98/128 baseline

## Baseline

- Source: `source-review/f6074`
- Commit snapshot: `f6074ba88762ac14f4a9f7da011c2a11f23989fc`
- Authoritative score: 98/128, with 30 wrong samples
- Dataset: filtered seed-4 128
- Model and decoding: Qwen3.5-9B, temperature 0

## Only behavioral change

The existing fused `MEMORY_GROUNDED` call now separates two decisions:

1. Is a saved fact faithful to its cited source?
2. Does that source-backed fact entail the proposed binding for the exact rendered query?

Before BIND/REBIND, the prompt requires an exact
`SUBJECT --REQUESTED RELATION--> VALUE` claim. It explicitly rejects cross-subject,
cross-relation, reversed-direction, wrong-type and proxy-attribute bindings. Hints
remain recorded but are navigation clues, not binding evidence.

This version does not add another model call. UPDATE, HINT storage, RECALL,
REBIND eligibility, ANSWER, runtime state transitions and evidence packaging are
unchanged. This makes the experiment attributable to VERIFY semantics rather
than additional calls or routing changes.

## Intended failures to block

- `dev_4478`: education location must not become birthplace.
- `dev_4961`: birth country plus death date must not become death place.
- `dev_4020`: another couple's husband relation must not answer Kaarin Fairfax's husband query.
- `dev_4311`: film country must not become director birthplace.
- `dev_11393`: nationality must not become birthplace.
- `dev_9193`: film release years must not become directors' birth years.

## Run

```bash
python scripts/ultimate/run_experiment.py \
  --config configs/ultimate/v08_verify_step1_full128.json \
  --expected-samples 128
```

Set `MODEL_API_KEY` in the environment or enter it when the wrapper prompts.
