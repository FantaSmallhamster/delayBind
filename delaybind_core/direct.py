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
