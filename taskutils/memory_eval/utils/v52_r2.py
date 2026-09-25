"""R2 adapter for the long-context QA evaluation entry point."""

import os
from pathlib import Path
from uuid import uuid4

from delaybind_core.api import APIConfig, OpenAICompatibleClient
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.storage import SQLiteEventStore


async def async_query_llm(item, model, tokenizer, temperature=0.7, top_p=0.95, stop=None, nocallback=False):
    from .envs import API_KEY, URL, RECURRENT_CHUNK_SIZE, RECURRENT_MAX_CONTEXT_LEN, RECURRENT_MAX_NEW

    run_id = f"v5.2-r2-{uuid4()}"
    output_dir = os.getenv("V52_LOG_DIR")
    if output_dir:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    store = SQLiteEventStore(str(Path(output_dir) / f"{run_id}.sqlite") if output_dir else ":memory:")

    class EvaluationClient(OpenAICompatibleClient):
        async def complete(self, **kwargs):
            sampling = {"top_p": top_p}
            if stop is not None:
                sampling["stop"] = stop
            kwargs["extra"] = {**sampling, **(kwargs.get("extra") or {})}
            return await super().complete(**kwargs)

    client = EvaluationClient(APIConfig(base_url=URL, api_key=API_KEY, model=model,
                                        temperature=temperature, top_p=top_p, seed=None,
                                        max_tokens=RECURRENT_MAX_NEW), store=store)
    config = RunnerConfig(
        protocol_version="v5.2-r2", plan_format="subqueries", answer_format="boxed",
        plan_repair_mode=os.getenv("V52_R2_PLAN_REPAIR_MODE", "disabled"),
        max_rebind_sessions_per_window=int(os.getenv("V52_R2_MAX_REBIND_SESSIONS", "64")),
        chunk_size=RECURRENT_CHUNK_SIZE, enable_defer_callback=not nocallback,
        memory_token_budget=int(os.getenv("V52_MEMORY_TOKENS", "8192")),
        window_mode=os.getenv("V52_WINDOW_MODE", "sentence"),
        sentence_splitting=RunnerConfig.from_mapping({
            "sentence_splitting": os.getenv("V52_SENTENCE_SPLITTING", "true")}).sentence_splitting,
        max_review_input_tokens=int(os.getenv("V52_MAX_REVIEW_INPUT_TOKENS", str(RECURRENT_MAX_CONTEXT_LEN))),
        max_input_tokens=RECURRENT_MAX_CONTEXT_LEN, max_answer_input_tokens=RECURRENT_MAX_CONTEXT_LEN,
        max_context_tokens=RECURRENT_MAX_CONTEXT_LEN + RECURRENT_MAX_NEW,
        max_recall_candidates=int(os.getenv("V52_MAX_RECALL_CANDIDATES", "1000")),
        max_context_expansions=int(os.getenv("V52_MAX_CONTEXT_EXPANSIONS", "8")),
        max_memory_rounds_per_window=int(os.getenv("V52_MAX_MEMORY_ROUNDS", "128")),
        max_model_calls=int(os.getenv("V52_MAX_MODEL_CALLS", "1000")),
    )
    try:
        result = await V5Runner(client, config=config, tokenizer=tokenizer).run(
            run_id=run_id, item=item, store=store)
        if result["status"] in {"RESOURCE_LIMIT", "RUNTIME_ERROR"}:
            raise RuntimeError(f"v5.2-r2 {result['status']}: {result['reason_codes']}")
        return (result.get("answer") or {}).get("raw_response", "\\boxed{UNKNOWN}")
    finally:
        store.close()
