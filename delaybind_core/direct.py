"""Direct Full Context baseline using the same OpenAI-compatible client."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .api import OpenAICompatibleClient
from .manifest import Manifest
from .prompts import direct_prompt
from .schema import AnswerResponse
from .storage import SQLiteEventStore


class DirectFullContextError(RuntimeError):
    """Raised when the direct baseline cannot produce a typed answer."""


async def run_direct_full_context(
    *,
    run_id: str,
    question: str,
    manifest: Manifest,
    client: OpenAICompatibleClient,
    store: SQLiteEventStore,
) -> dict[str, Any]:
    """Send the entire manifest in one prompt and return an auditable result."""
    entries = [entry.model_dump(mode="json") for entry in manifest.entries]
    schema = AnswerResponse.model_json_schema()
    raw = await client.complete(
        run_id=run_id,
        interface="ANSWER",
        messages=[
            {
                "role": "user",
                "content": direct_prompt(question, entries, schema=schema),
            }
        ],
        response_schema=schema,
    )
    try:
        answer = AnswerResponse.model_validate_json(raw)
    except ValidationError as exc:
        raise DirectFullContextError(f"direct output does not match AnswerResponse: {exc}") from exc
    return {
        "run_id": run_id,
        "method": "direct_full_context",
        "question": question,
        "answer": answer.model_dump(mode="json"),
        "manifest_id": manifest.manifest_id,
        "context_entries": len(entries),
        "model_calls": len(
            store.connection.execute(
                "SELECT 1 FROM model_calls WHERE run_id=?", (run_id,)
            ).fetchall()
        ),
    }


async def run_direct_mwr(*, run_id, question, manifest, client, store, config, tokenizer,
                         evidence_bundle=None, sample=None):
    """Direct under the exact same answer contract; the legacy entry stays frozen."""
    from .mwr.answering import AnswerContract, solve_answer
    from .mwr.budget import CallBudget
    from .mwr.common import run_contract, digest
    from .mwr.models import InferenceInput
    from .mwr.source_access import SourceAccess
    from .schema import RuntimeEvent
    from uuid import uuid4
    bundle = evidence_bundle
    if bundle is None:
        # Raw-context experiments must use the same document boundaries and
        # stable source IDs as MWR. A legacy manifest wraps all 50 documents in
        # one entry and would otherwise invent a different citation granularity.
        if sample is not None and sample.context is not None:
            inp = InferenceInput.from_sample(sample)
            access = SourceAccess(inp, tokenizer, neighbor_anchors=0)
        else:
            inp = InferenceInput(sample_id=manifest.sample_id, question=question)
            access = SourceAccess(inp, tokenizer, manifest=manifest, neighbor_anchors=0)
        sources = []
        while not access.exhausted:
            sources.extend(access.next_window(config.chunk_size))
        bundle = {"raw_sources": sources, "supported_relations": [], "unresolved_demands": [], "conflicts": []}
    contract = run_contract(config, question, bundle, client, None, tokenizer)
    def log(kind, payload):
        store.append_runtime_event(RuntimeEvent(run_id=run_id, event_id=str(uuid4()), event_type=kind, payload=payload))
    calls = CallBudget(client, None, tokenizer, config, run_id, digest(contract), log)
    # Namespace belongs to resource/cache metadata; the solver content is identical.
    answer = await solve_answer(question, bundle, AnswerContract(), calls, config.mwr)
    return {"run_id": run_id, "engine": "direct_mwr", "schema_version": "delaybind-mwr-v1",
            "answer": answer.model_dump(), "evidence_pack": {**bundle, "answer_value": answer.answer},
            "state": {"status": answer.status}, "status": answer.status,
            "logical_model_calls": calls.calls}
