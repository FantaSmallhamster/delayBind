"""Small stateless OpenAI-compatible API client with deterministic bookkeeping."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from .schema import ModelCall
from .storage import SQLiteEventStore


class ModelAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class APIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    seed: int | None = 4
    timeout_seconds: float = 120.0
    max_retries: int = 2
    max_tokens: int = 4096
    enable_thinking: bool = False


class OpenAICompatibleClient:
    def __init__(self, config: APIConfig, *, store: SQLiteEventStore | None = None):
        self.config = config
        self.store = store

    def _request_payload(
        self,
        interface: str,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "enable_thinking": self.config.enable_thinking,
        }
        if self.config.seed is not None:
            payload["seed"] = self.config.seed
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": interface.lower(), "schema": response_schema},
            }
        if extra:
            payload.update(extra)
        return payload

    @staticmethod
    def _extract_content(response: dict[str, Any]) -> str:
        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelAPIError("OpenAI-compatible response has no choices[0].message.content") from exc

    def _call_sync(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
            },
        )
        started = time.perf_counter()
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise ModelAPIError(str(exc)) from exc
        return result, (time.perf_counter() - started) * 1000.0

    async def complete(
        self,
        *,
        run_id: str,
        interface: str,
        messages: list[dict[str, str]],
        response_schema: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        payload = self._request_payload(interface, messages, response_schema=response_schema, extra=extra)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        prompt_hash = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.store is not None:
            cached = self.store.find_cached_model_call(run_id, request_hash)
            if cached is not None and cached.parsed_output is not None and cached.error is None:
                self.store.append_model_call(
                    cached.model_copy(
                        update={
                            "call_id": str(uuid4()),
                            "attempt": 0,
                            "cache_hit": True,
                        }
                    )
                )
                return str(cached.parsed_output)
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 2):
            call_id = str(uuid4())
            try:
                response, latency_ms = await asyncio.to_thread(self._call_sync, payload)
                content = self._extract_content(response)
                if self.store is not None:
                    usage = response.get("usage") or {}
                    self.store.append_model_call(
                        ModelCall(
                            call_id=call_id,
                            run_id=run_id,
                            interface=interface,  # type: ignore[arg-type]
                            request_hash=request_hash,
                            model=self.config.model,
                            parameters=payload,
                            prompt_hash=prompt_hash,
                            raw_request=payload,
                            raw_response=response,
                            parsed_output=content,
                            attempt=attempt,
                            latency_ms=latency_ms,
                            input_tokens=usage.get("prompt_tokens"),
                            output_tokens=usage.get("completion_tokens"),
                        )
                    )
                return content
            except Exception as exc:  # API adapters must preserve each failed attempt in future revisions.
                last_error = exc
                if self.store is not None:
                    self.store.append_model_call(
                        ModelCall(
                            call_id=call_id,
                            run_id=run_id,
                            interface=interface,  # type: ignore[arg-type]
                            request_hash=request_hash,
                            model=self.config.model,
                            parameters=payload,
                            prompt_hash=prompt_hash,
                            raw_request=payload,
                            raw_response=None,
                            parsed_output=None,
                            attempt=attempt,
                            error=str(exc),
                        )
                    )
                if attempt > self.config.max_retries:
                    break
                await asyncio.sleep(min(2**(attempt - 1), 4))
        raise ModelAPIError(f"{interface} failed after retries: {last_error}") from last_error
