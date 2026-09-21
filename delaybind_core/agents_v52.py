"""Role capabilities enforced before transport; legacy runner stays independent."""


class RestrictedAgent:
    interfaces = frozenset()

    def __init__(self, client):
        self.client = client

    async def call(self, interface: str, **kwargs):
        if interface not in self.interfaces:
            raise PermissionError(f"INTERFACE_NOT_AUTHORIZED:{type(self).__name__}:{interface}")
        return await self.client.complete(interface=interface, agent_role=self.role, **kwargs)


class LowLevelAgentV52(RestrictedAgent):
    role = "LOW"
    interfaces = frozenset({"UPDATE", "UPDATE_REPAIR", "RECALL"})


class HighLevelAgentV52(RestrictedAgent):
    role = "HIGH"
    interfaces = frozenset({"PLAN", "MEMORY", "MEMORY_REPAIR", "ANSWER"})
