import asyncio

from delaybind_core.api import APIConfig, OpenAICompatibleClient
from delaybind_core.storage import SQLiteEventStore


def test_api_failures_and_cache_hits_are_recorded(monkeypatch):
    store = SQLiteEventStore()
    client = OpenAICompatibleClient(
        APIConfig(base_url="http://unused", api_key="x", model="mock", max_retries=1),
        store=store,
    )
    calls = {"count": 0}

    def fake_call(_payload):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary")
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}, 1.0

    monkeypatch.setattr(client, "_call_sync", fake_call)
    result = asyncio.run(
        client.complete(
            run_id="r",
            interface="UPDATE",
            messages=[{"role": "user", "content": "hello"}],
        )
    )
    assert result == '{"ok":true}'
    rows = store.connection.execute(
        "SELECT attempt, payload_json FROM model_calls WHERE run_id=? ORDER BY attempt", ("r",)
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]

    before = calls["count"]
    cached = asyncio.run(
        client.complete(
            run_id="r",
            interface="UPDATE",
            messages=[{"role": "user", "content": "hello"}],
        )
    )
    assert cached == result
    assert calls["count"] == before
    assert store.connection.execute(
        "SELECT COUNT(*) FROM model_calls WHERE run_id=? AND attempt=0", ("r",)
    ).fetchone()[0] == 1
