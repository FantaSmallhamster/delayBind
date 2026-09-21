"""V5.1 adapter for the existing ReMemR1 memory_eval input and boxed output."""

from __future__ import annotations

import os

from delaybind_core.api import APIConfig, OpenAICompatibleClient
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.storage import SQLiteEventStore


async def async_query_llm(item, model, tokenizer, temperature=0.7, top_p=0.95, stop=None, nocallback=False):
    # Import the original evaluation endpoint/settings only when invoked.
    from .envs import URL, API_KEY, RECURRENT_CHUNK_SIZE, RECURRENT_MAX_NEW

    class EvaluationClient(OpenAICompatibleClient):
        async def complete(self, **kwargs):
            sampling = {"top_p": top_p}
            if stop is not None:
                sampling["stop"] = stop
            kwargs["extra"] = {**sampling, **(kwargs.get("extra") or {})}
            return await super().complete(**kwargs)

    store = SQLiteEventStore()
    client = EvaluationClient(APIConfig(
        base_url=URL, api_key=API_KEY, model=model, temperature=temperature,
        top_p=top_p, seed=None, max_tokens=RECURRENT_MAX_NEW,
    ), store=store)
    runner = V5Runner(client, tokenizer=tokenizer, config=RunnerConfig(
        protocol_version="v5.1",
        chunk_size=RECURRENT_CHUNK_SIZE, answer_format="boxed",
        max_model_calls=int(os.environ.get("V51_MAX_MODEL_CALLS", "1000")),
        memory_token_budget=int(os.environ.get("V51_MEMORY_TOKENS", "8192")),
        enable_defer_callback=not nocallback,
    ))
    try:
        result = await runner.run(run_id=f"v51-{item.get('_id', 'sample')}", item=item, store=store)
        return result["raw_answer"] or ""
    finally:
        store.close()
