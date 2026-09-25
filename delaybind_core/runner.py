"""R2 runner and persisted run configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import OpenAICompatibleClient
from .cursor import TextTokenizer
from .data import CanonicalSample, canonicalize_record
from .manifest import Manifest
from .schema_r2 import EvidencePlanR2
from .schema_v52 import QueryPlanV3
from .storage import SQLiteEventStore


# Persist these former settings in fingerprints so existing R2 runs can resume.
_PERSISTED_CONFIG_DEFAULTS = {
    "answer_mode": "runtime", "max_verify_candidates": 1000,
    "snapshot_every_windows": 1, "verify_committed": True,
    "max_graph_claims": 128, "max_verify_expansions": 1,
    "verify_expansion_limit": 32, "require_evidence_sources": True,
    "min_streaming_windows": 0, "query_graph_mode": "open",
    "require_source_span": True, "callback_retrieval_limit": 16,
    "max_targeted_updates": 16, "verify_source_neighborhood": 0,
    "max_response_retries": 1,
}


@dataclass(frozen=True)
class RunnerConfig:
    protocol_version: str = "v5.2-r2"
    plan_format: str = "subqueries"
    chunk_size: int = 5000
    max_windows: int = 100000
    max_plan_retries: int = 1
    max_model_calls: int = 1000
    defer_unbound: bool = True
    candidate_batch_size: int = 32
    memory_token_budget: int | None = None
    memory_char_budget: int = 24000
    answer_format: str = "auto"
    enable_defer_callback: bool = True
    sentence_splitting: bool = True
    window_mode: str = "sentence"
    tokenizer_encoding: str = "cl100k_base"
    max_recall_candidates: int = 1000
    max_review_input_tokens: int = 32768
    max_input_tokens: int = 32768
    max_answer_input_tokens: int = 32768
    max_context_tokens: int = 65536
    max_memory_rounds_per_window: int = 128
    max_context_expansions: int = 8
    source_context_before: int = 1
    source_context_after: int = 1
    answer_reserve_calls: int = 1
    answer_reserve_tokens: int = 512
    max_protocol_retries: int = 1
    memory_contract: str = "bind-rebind-1"
    rebind_policy: str = "raw_review"
    memory_batching: str = "query_scoped"
    proof_change_policy: str = "invalidate_descendants"
    plan_repair_mode: str = "disabled"
    max_rebind_sessions_per_window: int = 64
    update_input_mode: str = "archive_windows"
    plan_repair_failure_policy: str = "abort"
    memory_interface: str = "legacy_bind_rebind_v1"

    def protocol_mapping(self):
        """Keep the persisted R2 fingerprint stable across resumed runs."""
        from dataclasses import asdict
        result = {**_PERSISTED_CONFIG_DEFAULTS, **asdict(self)}
        if self.update_input_mode == "archive_windows":
            result.pop("update_input_mode")
        if self.plan_repair_failure_policy == "abort":
            result.pop("plan_repair_failure_policy")
        if self.memory_interface == "legacy_bind_rebind_v1":
            result.pop("memory_interface")
        return result

    def __post_init__(self) -> None:
        if not isinstance(self.sentence_splitting, bool):
            raise ValueError("sentence_splitting must be a boolean")
        if self.protocol_version != "v5.2-r2":
            raise ValueError("only protocol_version=v5.2-r2 is supported")
        if self.plan_format != "subqueries":
            raise ValueError("R2 requires plan_format=subqueries")
        if self.window_mode not in {"sentence", "fragment"}:
            raise ValueError("invalid window_mode")
        for name in ("max_recall_candidates", "max_review_input_tokens", "max_input_tokens",
                     "max_answer_input_tokens", "max_context_tokens", "max_memory_rounds_per_window",
                     "answer_reserve_calls", "answer_reserve_tokens"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        for name in ("max_context_expansions", "source_context_before", "source_context_after", "max_protocol_retries"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if (self.memory_contract, self.rebind_policy, self.memory_batching, self.proof_change_policy) != (
                "bind-rebind-1", "raw_review", "query_scoped", "invalidate_descendants"):
            raise ValueError("unsupported R2 contract or policy")
        if self.plan_repair_mode not in {"disabled", "on_hint"} or self.max_rebind_sessions_per_window <= 0:
            raise ValueError("invalid R2 repair mode or review session budget")
        if self.update_input_mode not in {"archive_windows", "plain_token_chunks"}:
            raise ValueError("invalid update_input_mode")
        if self.plan_repair_failure_policy not in {"abort", "continue_valid_plan"}:
            raise ValueError("invalid plan_repair_failure_policy")
        if self.memory_interface not in {"legacy_bind_rebind_v1", "unified_evidence_v1"}:
            raise ValueError("invalid memory_interface")
        if self.memory_interface == "unified_evidence_v1" and self.sentence_splitting:
            raise ValueError("unified_evidence_v1 requires fact-only R2")
        if self.update_input_mode == "plain_token_chunks" and self.sentence_splitting:
            raise ValueError("plain_token_chunks requires fact-only R2")
        if self.candidate_batch_size <= 0 or self.chunk_size <= 0:
            raise ValueError("candidate_batch_size and chunk_size must be positive")
        if self.answer_format not in {"auto", "boxed", "text"}:
            raise ValueError("answer_format must be auto, boxed, or text")
        if self.memory_char_budget <= 0 or self.memory_token_budget is not None and self.memory_token_budget <= 0:
            raise ValueError("memory budgets must be positive")

    @classmethod
    def from_mapping(cls, value):
        from dataclasses import fields
        obsolete = set(value) & _PERSISTED_CONFIG_DEFAULTS.keys()
        if obsolete:
            raise ValueError(f"obsolete runner settings: {sorted(obsolete)}")
        values = {}
        for field in fields(cls):
            if field.name not in value:
                continue
            raw = value[field.name]
            if field.name == "sentence_splitting":
                if isinstance(raw, str) and raw.lower() in {"true", "false", "1", "0", "on", "off"}:
                    raw = raw.lower() in {"true", "1", "on"}
                if not isinstance(raw, bool):
                    raise ValueError("sentence_splitting must be true or false")
                values[field.name] = raw
                continue
            if str(field.type) == "int | None":
                values[field.name] = int(raw) if raw is not None else None
            else:
                convert = {"int": int, "bool": bool, "str": str}.get(str(field.type))
                values[field.name] = convert(raw) if convert else raw
        return cls(**values)


class V5Runner:
    """Public runner name retained for existing R2 experiment integrations."""

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
        plan: QueryPlanV3 | EvidencePlanR2 | None = None,
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
                raise ValueError("provide sample, a ReMemR1 item, or question and context")
        elif item is not None or question is not None or context is not None:
            raise ValueError("choose one input form")
        if any(event.schema_version != "v5.2-r2" for event in store.list_runtime_events(run_id)):
            raise ValueError("RUN_PROTOCOL_VERSION_MISMATCH: use a new run_id for a new protocol")
        from .subquery_runner_r2 import run_subqueries_r2
        return await run_subqueries_r2(
            self, run_id=run_id, sample=sample, manifest=manifest, store=store, plan=plan
        )


def load_manifest(path: str | Path) -> Manifest:
    return Manifest.from_json(path)
