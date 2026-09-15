"""Two agent roles with separate interfaces and injectable model clients."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .api import OpenAICompatibleClient


class Agent:
    role: str
    interfaces: frozenset[str]

    def __init__(self, client: OpenAICompatibleClient, invoke: Callable[..., Awaitable[Any]]):
        self.client = client
        self._invoke = invoke

    async def call(self, interface: str, prompt: str, **kwargs: Any) -> Any:
        if interface not in self.interfaces:
            raise ValueError(f"{self.role} agent cannot call {interface}")
        return await self._invoke(self.client, self.role, interface, prompt, **kwargs)


class HighLevelAgent(Agent):
    role = "HIGH"
    interfaces = frozenset({"PLAN", "MEMORY", "ANSWER"})


class LowLevelAgent(Agent):
    role = "LOW"
    interfaces = frozenset({"UPDATE", "RECALL", "VERIFY"})
