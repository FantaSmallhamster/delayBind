"""Small stateless OpenAI-compatible API client with deterministic bookkeeping."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import ipaddress
import socket
from http.client import HTTPSConnection
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from uuid import uuid4

from .schema import ModelCall
from .storage import SQLiteEventStore


class ModelAPIError(RuntimeError):
    pass


class ModelCompletion(str):
    """String-compatible response carrying provider completion bookkeeping."""

    def __new__(cls, content, *, finish_reason=None, input_tokens=None, output_tokens=None):
        result = super().__new__(cls, content)
        result.finish_reason = finish_reason
        result.input_tokens = input_tokens
        result.output_tokens = output_tokens
        return result


@dataclass(frozen=True)
class APIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    top_p: float | None = None
    seed: int | None = 4
    timeout_seconds: float = 120.0
    max_retries: int = 2
    max_tokens: int = 4096
    enable_thinking: bool = False
    connect_ip: str | None = None


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
        if self.config.top_p is not None:
            payload["top_p"] = self.config.top_p
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

    @classmethod
    def _completed_content(cls, response: dict[str, Any], *, require_complete=False) -> ModelCompletion:
        content = cls._extract_content(response)
        if not isinstance(content, str):
            raise ModelAPIError("OpenAI-compatible response content is not text")
        reason = response["choices"][0].get("finish_reason")
        if require_complete and reason != "stop":
            raise ModelAPIError(f"INCOMPLETE_MODEL_RESPONSE:{reason or 'missing_finish_reason'}")
        usage = response.get("usage") or {}
        return ModelCompletion(content, finish_reason=reason,
                               input_tokens=usage.get("prompt_tokens"),
                               output_tokens=usage.get("completion_tokens"))

    def _call_sync(self, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            url = base
        elif base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.config.connect_ip is not None:
            return self._call_fixed_address(url, body)
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

    def _call_fixed_address(self, url: str, body: bytes) -> tuple[dict[str, Any], float]:
        """Select a connection IP while retaining the URL's Host and TLS SNI.

        This affects only this connection; system DNS and other clients are
        untouched. HTTPSConnection still checks the original hostname's cert.
        """
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ModelAPIError("connect_ip requires an HTTPS URL")
        address = str(ipaddress.ip_address(self.config.connect_ip))
        port = parsed.port or 443
        connection = HTTPSConnection(parsed.hostname, port, timeout=self.config.timeout_seconds)
        connection._create_connection = lambda _endpoint, timeout, source_address=None: socket.create_connection(
            (address, port), timeout=timeout, source_address=source_address,
        )
        started = time.perf_counter()
        try:
            path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
            connection.request("POST", path, body=body, headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {self.config.api_key}",
            })
            peer_ip = connection.sock.getpeername()[0]
            response = connection.getresponse()
            raw = response.read()
            if response.status >= 400:
                raise ModelAPIError(f"HTTP {response.status}: {raw.decode(errors='replace')[:1000]}")
            result = json.loads(raw)
            result["_client_transport"] = {
                "connect_ip": address, "peer_ip": peer_ip, "tls_hostname": parsed.hostname,
                "http_status": response.status,
                "trace_id": response.getheader("x-siliconcloud-trace-id"),
            }
            return result, (time.perf_counter() - started) * 1000.0
        finally:
            connection.close()

    async def complete(
        self,
        *,
        run_id: str,
        interface: str,
        messages: list[dict[str, str]],
        response_schema: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        agent_role: str | None = None,
        local_metadata: dict[str, Any] | None = None,
    ) -> str:
        if self.store is not None and self.store.connection.in_transaction:
            raise RuntimeError("MODEL_CALL_DURING_RUNTIME_TRANSACTION")
        audit_fields = {"protocol_version", "memory_mode", "review_phase", "context_id", "review_id", "raw_bundle_hash"}
        if local_metadata is not None and set(local_metadata) - (audit_fields | {"memory_interface_version"}):
            raise ValueError("UNKNOWN_LOCAL_AUDIT_FIELD")
        audit = {key: value for key, value in (local_metadata or {}).items() if key in audit_fields}
        require_complete = (interface in {"MEMORY", "MEMORY_REPAIR"}
                            and (local_metadata or {}).get("memory_interface_version") == "query-facts-result-v1")
        payload = self._request_payload(interface, messages, response_schema=response_schema, extra=extra)
        cache_identity = {"payload": payload, "agent_role": agent_role}
        if local_metadata:
            cache_identity["local_metadata"] = local_metadata
        serialized = json.dumps(cache_identity, ensure_ascii=False, sort_keys=True)
        request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        prompt_hash = hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if self.store is not None:
            cached = self.store.find_cached_model_call(run_id, request_hash)
            if cached is not None and cached.parsed_output is not None and cached.error is None:
                completed = (self._completed_content(cached.raw_response, require_complete=True)
                             if require_complete else ModelCompletion(
                                 str(cached.parsed_output), input_tokens=cached.input_tokens,
                                 output_tokens=cached.output_tokens))
                self.store.append_model_call(
                    cached.model_copy(
                        update={
                            "call_id": str(uuid4()),
                            "attempt": 0,
                            "cache_hit": True,
                        }
                    )
                )
                return completed
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 2):
            call_id = str(uuid4())
            response, latency_ms = None, None
            try:
                response, latency_ms = await asyncio.to_thread(self._call_sync, payload)
                content = self._completed_content(response, require_complete=require_complete)
                if self.store is not None:
                    usage = response.get("usage") or {}
                    self.store.append_model_call(
                        ModelCall(
                            **audit,
                            call_id=call_id,
                            run_id=run_id,
                            interface=interface,  # type: ignore[arg-type]
                            agent_role=agent_role,
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
            except Exception as exc:
                last_error = exc
                if self.store is not None:
                    usage = (response or {}).get("usage") or {}
                    self.store.append_model_call(
                        ModelCall(
                            **audit,
                            call_id=call_id,
                            run_id=run_id,
                            interface=interface,  # type: ignore[arg-type]
                            agent_role=agent_role,
                            request_hash=request_hash,
                            model=self.config.model,
                            parameters=payload,
                            prompt_hash=prompt_hash,
                            raw_request=payload,
                            raw_response=response,
                            parsed_output=None,
                            attempt=attempt,
                            latency_ms=latency_ms,
                            input_tokens=usage.get("prompt_tokens"),
                            output_tokens=usage.get("completion_tokens"),
                            error=str(exc),
                        )
                    )
                if attempt > self.config.max_retries:
                    break
                await asyncio.sleep(min(2**(attempt - 1), 4))
        raise ModelAPIError(f"{interface} failed after retries: {last_error}") from last_error
