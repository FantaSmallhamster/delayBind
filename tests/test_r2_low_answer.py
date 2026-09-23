"""R2 roles are independent of the shared V5.2 agent classes and transports."""

import asyncio

import pytest

from delaybind_core.agents_r2 import HighLevelAgentR2, LowLevelAgentR2
from delaybind_core.agents_v52 import HighLevelAgentV52
from delaybind_core.data import canonicalize_record, build_manifest
from delaybind_core.runner import RunnerConfig, V5Runner
from delaybind_core.smoke_r2 import ScriptedR2Client
from delaybind_core.storage import SQLiteEventStore


class Spy(ScriptedR2Client):
    def __init__(self):
        super().__init__()
        self.roles = []

    async def complete(self, **kwargs):
        self.roles.append((kwargs["interface"], kwargs["agent_role"]))
        return await super().complete(**kwargs)


@pytest.mark.parametrize("raw", [False, True])
def test_answer_uses_reader_client_and_low_role(raw):
    high, low = Spy(), Spy()
    sample = canonicalize_record(dict(id="roles", question="Where was Cindy's teacher's mother born?",
        context=[["Mary", ["Mary was born in Suzhou."]], ["Alice", ["Alice's mother is Mary."]],
                 ["Cindy", ["Cindy's teacher is Alice."]]]))
    result = asyncio.run(V5Runner(high, reader_client=low,
        config=RunnerConfig(protocol_version="v5.2-r2", sentence_splitting=raw,
                            chunk_size=32)).run(run_id="roles-" + str(raw), sample=sample,
                                                  manifest=build_manifest(sample), store=SQLiteEventStore()))
    assert result["status"] == "ANSWERED", result["reason_codes"]
    assert high.roles and all(role == "HIGH" and interface in {"PLAN", "MEMORY", "MEMORY_REPAIR"}
                              for interface, role in high.roles)
    assert any(interface == "ANSWER" for interface, _ in low.roles)
    assert all(role == "LOW" and interface in {"UPDATE", "UPDATE_REPAIR", "RECALL", "ANSWER"}
               for interface, role in low.roles)
    assert result["config"]["answer_agent_role"] == "LOW"
    answer = next(m for m in result["context_manifests"] if m.get("interface") == "ANSWER")
    assert answer["agent_role"] == "LOW"


def test_r2_rejects_cross_role_calls_without_changing_v52():
    low, high = LowLevelAgentR2(Spy()), HighLevelAgentR2(Spy())
    assert "ANSWER" in low.interfaces and "ANSWER" not in high.interfaces
    assert "ANSWER" in HighLevelAgentV52.interfaces
    for agent, interface in ((low, "MEMORY"), (low, "PLAN"), (high, "ANSWER"), (high, "VERIFY")):
        with pytest.raises(PermissionError):
            asyncio.run(agent.call(interface))
