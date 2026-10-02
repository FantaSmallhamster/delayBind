"""V08 runner for the delayed-binding subquery runtime."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .api import OpenAICompatibleClient
from .cursor import TextTokenizer
from .data import CanonicalSample, canonicalize_record
from .manifest import Manifest
from .schema import QueryPlan
from .storage import SQLiteEventStore


@dataclass(frozen=True)
class RunnerConfig:
    plan_format: str = "subqueries"
    chunk_size: int = 5000
    max_windows: int = 100000
    max_verify_candidates: int = 1000
    max_plan_retries: int = 1
    max_model_calls: int = 1000
    snapshot_every_windows: int = 1
    defer_unbound: bool = True
    min_streaming_windows: int = 0
    candidate_batch_size: int = 32
    max_response_retries: int = 1
    memory_token_budget: int | None = None
    memory_char_budget: int = 24000
    answer_format: str = "auto"
    enable_defer_callback: bool = True
    memory_source_mode: str = "postverify"
    memory_source_token_budget: int = 8192
    update_protocol: str = "text"
    update_json_mode: str = "json_schema"
    memory_protocol: str = "text"
    memory_json_mode: str = "json_schema"

    def __post_init__(self) -> None:
        if self.candidate_batch_size <= 0 or self.chunk_size <= 0:
            raise ValueError("candidate_batch_size and chunk_size must be positive")
        if self.answer_format not in {"auto", "boxed", "text", "json"}:
            raise ValueError("answer_format must be auto, boxed, text, or json")
        if self.max_response_retries < 0:
            raise ValueError("retry limit must be nonnegative")
        if self.memory_char_budget <= 0 or self.memory_token_budget is not None and self.memory_token_budget <= 0:
            raise ValueError("memory budgets must be positive")
        if self.memory_source_mode not in {"postverify", "raw_before_memory", "source_verify_before_memory", "joint_source_memory"}:
            raise ValueError("memory_source_mode must be postverify, raw_before_memory, source_verify_before_memory, or joint_source_memory")
        if self.memory_source_token_budget <= 0:
            raise ValueError("memory_source_token_budget must be positive")
        if self.update_protocol not in {"text", "json"}:
            raise ValueError("update_protocol must be text or json")
        if self.update_json_mode not in {"json_schema", "json_object", "prompt"}:
            raise ValueError("update_json_mode must be json_schema, json_object, or prompt")
        if self.memory_protocol not in {"text", "json"}:
            raise ValueError("memory_protocol must be text or json")
        if self.memory_json_mode not in {"json_schema", "json_object", "prompt"}:
            raise ValueError("memory_json_mode must be json_schema, json_object, or prompt")


class ModelBudgetExceeded(RuntimeError):
    """Raised when a run reaches its configured logical model-call budget."""


class V5Runner:
    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        config: RunnerConfig | None = None,
        reader_client: OpenAICompatibleClient | None = None,
        tokenizer: TextTokenizer | None = None,
    ):
        self.client = client
        self.config = config or RunnerConfig()
        self.reader_client = reader_client
        self.tokenizer = tokenizer

    async def run(
        self,
        *,
        run_id: str,
        sample: CanonicalSample | None = None,
        manifest: Manifest | None = None,
        store: SQLiteEventStore,
        plan: QueryPlan | None = None,
        item: dict[str, Any] | None = None,
        question: str | None = None,
        context: str | None = None,
    ) -> dict[str, Any]:
        if item is not None and (question is not None or context is not None):
            raise ValueError("choose item or question/context, not both")
        if sample is None:
            if item is not None:
                sample = canonicalize_record(item)
            elif question is not None and context is not None:
                sample = CanonicalSample(sample_id=run_id, question=question, context=context)
            else:
                raise ValueError("provide sample, an item, or question and context")
        elif item is not None or question is not None or context is not None:
            raise ValueError("choose one input form")
        if self.config.plan_format != "subqueries":
            raise ValueError("V08 supports plan_format='subqueries' only")
        if plan is not None and not isinstance(plan, QueryPlan):
            raise ValueError("V08 requires a subquery plan")
        from .subquery_runner import run_subqueries

        return await run_subqueries(
            client=self.client, config=self.config, run_id=run_id,
            sample=sample, manifest=manifest, store=store, plan=plan,
            reader_client=self.reader_client, tokenizer=self.tokenizer,
        )


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.from_json(path)
