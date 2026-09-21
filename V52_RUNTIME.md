# DelayBind V5.2 raw-first subquery runtime

For the separately versioned text BIND/REBIND R2 state machine, see
[V52_R2_RUNTIME.md](V52_R2_RUNTIME.md). This page describes the retained six-action V5.2 path.

V5.2 implements the supplied `DelayBind_V5.2_原文优先双图绑定_详细修改设计.docx`
on the latest V5.1 `solitary-dingo` baseline, commit
`81dc262a22e523cd36113b8e3d8c9d9f544dc6ed`, matching the document's review baseline.
The pre-existing V5.1 implementation, frozen benchmark guards, tokenizer support,
high/low clients, adapter and historical results are preserved.

The default remains V5.1 subqueries; historical graph mode remains compatible. Select the
new method explicitly with `protocol_version="v5.2"` and
`plan_format="subqueries"`. V5.2 never invokes an independent VERIFY interface.

## Run

```bash
python -m pip install -e '.[test,v52]'
python -m scripts.run_v52_smoke
python -m pytest tests -q

# API-backed runs require dataset/model settings, as for the existing CLI.
python -m delaybind_core run --config configs/v52_raw_first_smoke.json
python -m delaybind_core experiment --config configs/v52_benchmark.json
```

The offline smoke fixture deliberately extracts “Shanghai” from a sentence
whose original text says “Suzhou”. It reads the three-hop evidence in reverse,
recalls candidates after binding, corrects the extraction through MEMORY, and
answers from the recovered original sentences. It uses a scripted client and
is an engineering test, not a measured accuracy or cost result.

`V5Runner(high_client, reader_client=reader_client, tokenizer=tokenizer, config=...)`
allows separate HIGH/LOW clients and the actual model tokenizer. Omitting
`reader_client` uses one model for both roles. The long-context QA entry point
supports `--api v52`; `run_eval.v52_config(checkpoint)` constructs an opt-in
configuration. A result filename containing `nocallback` disables historical
candidate recall without deleting deferred observations. Optional
`V52_LOG_DIR` retains each adapter run's SQLite audit database.

### 可选分句 / Optional sentence splitting

`sentence_splitting` defaults to `true`, preserving existing V5.2 runs. Set it
to `false` to use the actual V5.1 read cursors without punctuation segmentation;
this does not switch off the V5.2 binding/review protocol or change the model's
four-field text output into JSON.

```bash
# Both run and experiment accept these overrides; CLI takes precedence.
python -m delaybind_core experiment --config your-config.json --sentence-splitting
python -m delaybind_core experiment --config your-config.json --no-sentence-splitting
```

For a single-run config, set the top-level `sentence_splitting` boolean. For an
experiment config with a `runner` section, set `runner.sentence_splitting`.
The Python API uses `RunnerConfig(protocol_version="v5.2", sentence_splitting=False)`.
The existing QA adapter accepts `V52_SENTENCE_SPLITTING=false` (or `true`).

- **On:** use `syntok==1.4.4` through `segmenter.analyze()` with the V5.2 sentence
  index and `window_mode=sentence|fragment`, with `D18:S0` sentence/fragment anchors.
- **Off, raw text:** reuse V5.1 `TextReadCursor`. `chunk_size` counts tokenizer
  tokens when a tokenizer is supplied, otherwise Unicode characters, just as
  V5.1. Document headers identify portions inside each fixed window; `D18@C0`
  names document 18's portion of window 0. No sentence indexing runs. Anchor
  `kind=chunk` and `complete=true` mean the entire observed block is present,
  **not** that the block ends at a sentence boundary. The model must not invent
  missing continuations; HOLD and bounded neighboring-block context remain available.
- **Off, explicit/dataset manifest:** reuse V5.1 `ReadCursor`, keeping supplied
  entry boundaries and its word-budget behavior. Pre-existing sentence entries
  are neither split again nor merged. `window_mode` is ignored when splitting is off.

Chunk mode retains V5.2 immutable raw hashes, visibility checks, review and
binding proofs. If legacy tokenizer decoding changes an original slice (for
example a UTF-8 character split across token windows), it fails explicitly with
`LEGACY_CHUNK_NOT_VERBATIM` rather than labeling altered text as original evidence.
Use sentence mode or a lossless tokenizer/window boundary in that case.

The choice is included in config fingerprints, cursor snapshots and results.
Use a **new run ID / experiment ID and output directory** when changing modes;
resuming a run under a different mode is rejected. Historical paused outputs
and API smoke results have not been overwritten.

## Interfaces and execution

| Interface | Role | Authority |
| --- | --- | --- |
| PLAN | HIGH | Propose a v3 natural-language query DAG |
| UPDATE / UPDATE_REPAIR | LOW | Propose observations with visible sentence or legacy chunk anchors |
| RECALL | LOW | Select IDs from the current candidate batch, without raw text |
| MEMORY / MEMORY_REPAIR | HIGH | Review raw and propose one atomic increment |
| ANSWER | HIGH | Answer from question and raw-backed working memory |

For each window, the cursor registers immutable raw blocks and source indexes
before UPDATE. All valid observations are registered before maintenance begins.
Dormant queries retain lightweight candidates; active/resolved queries receive
PENDING uses. Every use requires MEMORY review before it can support a binding.

A binding creates a historical BindingRecord and updates query edges, working
memory ports, execution versions, invalidations and recall jobs in one SQLite
transaction. Both graphs reference the same binding ID and revision. A port can
exist before downstream evidence; no fact node is invented to fill that gap.

The scheduler scans every candidate batch before reviewing selected raw sources.
Review is also batched, with BIND disabled until the final batch. Pending,
conflicting or held evidence blocks binding. Complete-set bindings wait for EOF.
Maintenance reaches a stable state before advancing the read cursor. At EOF,
ANSWER still runs when queries remain unresolved. Operational/resource errors
remain errors even when a partial answer can be generated with reserved budget.

## Plan and memory contracts

Model-facing inputs are text sections with literal original-text blocks, not a
JSON payload. PLAN, UPDATE, RECALL and MEMORY retain the V5.1 text/line style.
The renderer uses the familiar Question / Plan / Working memory / Current window
sections, not recursive object/YAML serialization. Navigation uses fact and link
lines. Sources are shown immediately after their [D:S] anchor with kind,
completeness and original-character count; the body is not escaped or indented.

UPDATE receives the current accepted active view, pinned binding proofs and
reviewed conflicts together with their recovered original sources. FOCUS can
hide unrelated facts, but never a valid binding's proof. Dormant candidates,
unreviewed pending facts and future text are excluded. Its citable IDs are exactly
the union of displayed memory raw and current-window raw; repair uses the same
snapshot. The actual complete input and rendered memory are budgeted, and the
call audit includes sources from both sections. Duplicate anchors are shown once.
No structured-output JSON schema is sent to the API. ANSWER defaults to boxed,
including dataset inputs; the historical explicitly selected JSON answer option
remains available but is not used by the supplied V5.2 configurations.

```text
Q1
query: Who teaches Cindy?
output: ?teacher
depends_on: NONE

Q2
query: Who is ?teacher's mother?
output: ?mother
depends_on: Q1
```

The runtime checks unique outputs, input producer consistency, template variable
coverage, acyclicity and cardinality. The parser resolves template variables
against declared output producers and verifies depends_on, creating internal
v3 inputs/edges. Models do not output a schema version. Set values remain arrays;
implicit foreach is not supported. Optional cardinality: SET is a plan-block field.

MEMORY outputs ASSESS, CORRECT, BIND, UNBIND, PATCH, ROUTE and KEEP command lines.
The parser creates an internal operations object; schema_version and the exact
context ID come from the runtime, not the model. KEEP maps to internal FOCUS.

```text
Q1 | D3:S1 | Cindy's teacher is Alice. | ACTIVE
SELECT F1,F2
ASSESS | Q1 | F1 | ACCEPT | D3:S1 | RAW_SUPPORTED
BIND | Q1 | Alice | F1
KEEP | F1
```

The first two lines illustrate UPDATE and RECALL respectively, not MEMORY.
As in V5.1, individual typed BIND scalar/list values may contain JSON, but PATCH
no longer accepts a JSON field. It uses a plan-style text block instead:

```text
PATCH | Q4 | F1
query: Where was ?mother born?
output: ?city
depends_on: Q2
END PATCH
```

Existing queries need only changed fields; new queries need query/output and
their dependencies. The parser resolves inputs against the complete proposed
plan, validates dependencies and DAG structure, then submits the same atomic
operation as before. Unclosed blocks, duplicate fields and conflicting
dependencies fail the whole proposal without any state change.

No interface requires an entire JSON response. Facts and reasons escape
literal pipes/newlines as backslash-pipe/backslash-n. NONE explicitly means no output.
ASSESS supports ACCEPT, REJECT, HOLD and CONFLICT; acceptance is a scoped
`(query_id, input_signature, fact_id)` decision, not global fact truth.
Corrections create a new fact with `supersedes_fact_id`; local `N1` aliases can
be used by a BIND in the same transaction. Raw text and old facts remain intact.
PATCH validates the complete resulting DAG; affected queries cannot bind in
that response. ROUTE immediately schedules ready targets. FOCUS changes only
the view and cannot remove a valid binding's proof closure or its original text.

The full interface prompts live in `delaybind_core/agent_prompts_v52.py`, version
`v5.2-raw-first-lines-5`. Instructions are in a system message; input data is
rendered as V5.1-style named sections and original text. Untrusted document
content never grants authority. Old JSON-protocol smoke artifacts remain unchanged;
their run IDs/configuration fingerprints must not be reused for this protocol.

UPDATE renders an evidence-routing checklist from the query graph. Bridge facts
belong to their producer query; downstream attributes are separate observations.
Dormant named candidates remain deferred and cannot reverse-bind a parent.
This guides extraction but does not replace MEMORY's semantic review.

UPDATE_REPAIR is a bounded metadata/format repair, not a second extraction pass.
The runtime freezes targets from the original rejected items and allows at most
one replacement per target, preserving its fact/hint type and decoded claim text
(apart from surrounding whitespace). Query IDs, visible source references and
line syntax may be corrected. New or rewritten claims are rejected before ingest;
failed repair output never expands the target set. Valid rows survive retries,
including hints. Unrecoverable or unsupported targets may be omitted with NONE;
semantic corrections belong to MEMORY/CORRECT. All calls keep the original
window and visible-source snapshot and retain the four-column text protocol.

## Original text and permissions

Sentence anchors look like `D0:S0`; titles have `D0:H0`. Document aliases are
assigned from stable document identities, independently of reading order.
Dataset sentence numbers are preserved; dataset sentence coordinates are not
relabelled as coordinates in an artificially concatenated document.

`delaybind_core.cursor_v52.TextReadCursor` uses `syntok==1.4.4` sentence boundaries,
via `syntok.segmenter.analyze()` and original token offsets. There is no custom
sentence regex or abbreviation table, and no silent fallback to the old rules.
Install via `pip install -e '.[v52]'` (or `pip install syntok==1.4.4`). The dependency
is imported only when sentence splitting is used, so legacy chunk mode does not
require it. See the [official analyze/process distinction](https://github.com/fnl/syntok#syntoksegmenter).

The thin offset adapter retains leading whitespace in the first span, gaps
between sentences/paragraphs in the preceding span, and trailing whitespace in
the last span. Whitespace-only input remains one lossless span. It never rebuilds
raw text from token values, calls `process()`, normalizes whitespace, or uses
tokenizer.decode to construct sentence-mode evidence. Each source is an exact
`text[start:end]` slice and all slices concatenate to the full original input.
Numbered `Document N:` inputs preserve their original document number; `H0`
contains the exact header/title slice, with body coordinates relative to that
original document. Syntok is not guaranteed to segment every noisy passage
correctly and does not impose a sentence length limit. No wtpsplit/model download
or additional length-based segmentation is introduced. Sentence packing is the default. Fragment mode and oversized
sentences use stable `:P1`, `:P2` indexes; a complete anchor becomes readable only
when every character has entered the observed prefix. Fragment acceptance
requires a correction to a completed source; incomplete evidence can be held.

Text manifests and cursor checkpoints record `syntok-1.4.4-analyze-lossless-v1`.
Sentence numbers may differ from the old regex index, so start a new run/output
directory rather than resuming historical sentence-mode runs. Explicit dataset
sentence entries are still used as supplied, without re-segmentation.

The three distinct permission sets are the restored archive read watermark,
the current context's actually displayed sources, and currently accepted uses.
Bare document IDs, future anchors, cross-run records and invented aliases cannot
be substituted for visible sentence references. HOLD can request bounded,
same-document, already-read context. New neighboring sentences or completed
fragments reopen review. There is no V5.2 archive keyword-search capability.

ANSWER receives only the question, its output contract, and navigation,
raw_evidence and unresolved/conflict diagnostics. Original sentences are
deduplicated and hash checked. Deferred, unreviewed facts are excluded. JSON
citations must be visible in that final context. Boxed/text responses have empty
model citation lists unless the model actually supplies a supported structured
contract; `answer_context_source_refs` separately describes the context.

## Persistence and recovery

The additive SQLite migration introduces `raw_blocks`, `raw_sentences`,
`memory_transactions`, `recall_jobs` and `context_manifests`. Each successful
state transaction also records a checkpoint in `snapshots`. State events contain
structural state and source references, not repeated raw paragraphs.

The MEMORY transaction stages on a private state, checks graph invariants,
compares the expected database revision, persists the full event batch/jobs,
then publishes in-memory state. Failure leaves the previous state intact.
Committed event envelopes contain an event count and digest. Replay ignores an
uncommitted tail and rejects damaged committed batches or mixed protocols.

Restarting the same run restores cursor position, fragment blocks, pending
window, watermark and jobs. A crash between raw registration and the window
checkpoint safely re-observes the same immutable slices. A crash after binding
preserves the pending recall job. Completed runs do not issue duplicate calls.
Run configuration, question, source and tokenizer fingerprints prevent accidental
resume under a different experimental condition.

Historical v1 graph plans and v2 V5.1 subquery plans retain their original runtime
and event replay. V5.2 requires an explicit v3 plan and versioned transactions;
reuse of a run ID across protocols is rejected. Colliding V5.2 modules use an
`_v52` suffix; existing public V5.1 classes are not repurposed.

## Budgets and measurement

`chunk_size` includes window anchor overhead. Full serialized API messages are
counted for each interface; no output schema is attached. The default accounting
tokenizer is explicitly `cl100k_base`; pass the serving model's tokenizer for
model-specific accounting. Provider-specific chat framing overhead is not
measured by a tokenizer's plain `encode`, so keep headroom in
`max_context_tokens`. Server-reported usage is retained separately when supplied.

`memory_token_budget` bounds rendered working-memory text tokens when set;
otherwise `memory_char_budget` remains a character limit. These are separate
from the full serialized request limits. A required raw/proof bundle that cannot
fit produces an explicit error, without truncation.

Candidate counts, input tokens, review rounds and neighborhood expansions have
independent limits. ANSWER has reserved calls/output tokens. Oversized complete
proofs produce an explicit resource error; the runner does not truncate raw or
fall back to fact-only answers. Final evidence uses the complete current working
memory, regardless of FOCUS. Configure values for the actual model context size;
the example settings are not performance claims.

Results include raw coverage, binding proof coverage, scan completion, review
acceptance, dual-graph consistency, replay consistency, stale binding rate and
raw tokens per call. `anchor_valid_rate` is explicitly scoped to recoverability
of current accepted references. Semantic source accuracy is null without a
separate gold/manual audit. Zero VERIFY calls does not mean no source review.
V5.2 support-sentence scoring maps model citations back to original dataset
references; boxed/text answers do not acquire fabricated citations.

## Acceptance coverage

The acceptance matrix is implemented across `tests/test_memory_v52.py`,
`tests/test_v52_sources.py`, `tests/test_v52_runner.py` the dingo compatibility tests and all existing V5.1/graph tests.

| Design tests | Coverage |
| --- | --- |
| T01–T06 | PENDING review, reverse chain, update ordering, scoped uses, empty ports, immutable correction |
| T07–T09 | Raw shown for direction/negation/time/identity decisions; bounded pronoun context |
| T10–T14 | Rebinding, multi-parent signatures, idempotence, changed proof revisions, conflict barriers |
| T15–T19 | Failed recall, HOLD re-review, fragments/titles, no decode, strict source visibility |
| T20–T23 | Whole-proposal rollback, injected SQL failure, truncated replay, restored jobs, PATCH/ROUTE scheduling |
| T24–T28 | EOF set completeness, raw-first answer, empty memory, resource limits, no VERIFY |
| T29–T30 | Historical V5.1/graph regressions and stable identities across reading orders |

These are deterministic protocol tests. The runtime can enforce provenance,
permissions and atomic state transitions; it cannot prove a model's semantic
entailment judgment. Live model accuracy/cost benchmarks remain to be measured
with the supplied benchmark configuration and an available dataset/model.
