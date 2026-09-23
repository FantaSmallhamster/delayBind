"""R2-only role capabilities. Shared V5.2 permissions remain unchanged."""

from .agents_v52 import RestrictedAgent


class LowLevelAgentR2(RestrictedAgent):
    role = "LOW"
    interfaces = frozenset({"UPDATE", "UPDATE_REPAIR", "RECALL", "ANSWER"})


class HighLevelAgentR2(RestrictedAgent):
    role = "HIGH"
    interfaces = frozenset({"PLAN", "MEMORY", "MEMORY_REPAIR"})
