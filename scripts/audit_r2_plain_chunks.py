"""Offline reference-token, snapshot, and prompt-body equivalence audit."""

import argparse
import hashlib
import json
from pathlib import Path

from delaybind_core.context_r2 import build_chunk_update_context
from delaybind_core.cursor_plain_r2 import PlainTokenChunkCursor
from delaybind_core.data import canonicalize_record
from delaybind_core.prompts_r2 import messages
from delaybind_core.runner import RunnerConfig
from delaybind_core.runtime_r2 import RuntimeR2
from delaybind_core.schema_r2 import EvidencePlanR2
from delaybind_core.storage import SQLiteEventStore
from delaybind_core.tokenization import TokenizerJSON


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def audit(dataset, tokenizer_path, *, chunk_size):
    tokenizer = TokenizerJSON(tokenizer_path)
    config = RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=False,
                          update_input_mode="plain_token_chunks", chunk_size=chunk_size)
    plan = EvidencePlanR2(plan_id="audit", queries=[
        dict(id="Q1", template="What fact is stated?", output="?fact")])
    rows, total, mismatches = [], 0, 0
    for record in json.loads(Path(dataset).read_text(encoding="utf-8")):
        sample = canonicalize_record(record)
        reference_tokens = tokenizer.encode(sample.context.strip())
        cursor = PlainTokenChunkCursor(sample.context, tokenizer, chunk_size=chunk_size)
        store = SQLiteEventStore()
        runtime = RuntimeR2(run_id="audit-" + sample.sample_id, plan=plan, archive=None,
                            store=store, config=config)
        chunks = []
        while not cursor.exhausted:
            chunk = cursor.next_chunk()
            runtime.record_chunk(chunk, cursor.state())
            payload = build_chunk_update_context(runtime, sample.question, runtime.state.pending_chunk)
            snapshot = next(m for m in store.list_context_manifests(runtime.run_id)
                            if m.get("context_id") == payload["context_id"])
            request = messages("UPDATE", payload)[-1]["content"]
            expected = tokenizer.decode(reference_tokens[chunk.token_start:chunk.token_end])
            section = "<section>\n" + expected + "\n</section>"
            equal = (chunk.chunk_text == expected == snapshot["chunk_text"]
                     and section in request and chunk.token_end - chunk.token_start <= chunk_size)
            mismatches += not equal
            total += 1
            chunks.append(dict(index=chunk.chunk_index, token_start=chunk.token_start,
                               token_end=chunk.token_end, body_sha256=sha256(expected), equal=equal))
            runtime.finish_chunk()
        rows.append(dict(sample_id=sample.sample_id, context_sha256=sha256(sample.context),
                         token_count=len(reference_tokens), chunk_count=len(chunks), chunks=chunks))
        store.close()
    return dict(dataset=str(Path(dataset).resolve()), tokenizer=tokenizer.name_or_path,
                chunk_size=chunk_size, sample_count=len(rows), chunk_count=total,
                mismatch_count=mismatches, match_rate=(total - mismatches) / total if total else 1.0,
                samples=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = audit(args.dataset, args.tokenizer, chunk_size=args.chunk_size)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("sample_count", "chunk_count", "mismatch_count", "match_rate")}))
    if result["mismatch_count"]:
        raise SystemExit(1)
