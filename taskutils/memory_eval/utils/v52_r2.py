"""Opt-in R2 adapter; nocallback disables candidate recall, not resolved review."""
from .v52 import async_query_llm as _query


async def async_query_llm(item, model, tokenizer, temperature=0.7, top_p=0.95, stop=None, nocallback=False):
    return await _query(item, model, tokenizer, temperature, top_p, stop, nocallback, protocol_version="v5.2-r2")
