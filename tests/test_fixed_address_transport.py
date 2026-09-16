import json

import pytest

from delaybind_core import api


def test_fixed_ip_keeps_original_hostname_and_records_peer(monkeypatch):
    seen = {}

    class FakeSocket:
        def getpeername(self):
            return ("192.0.2.9", 443)

    def connect(endpoint, timeout, source_address):
        seen["endpoint"] = endpoint
        return FakeSocket()

    class FakeResponse:
        status = 200

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()

        def getheader(self, name):
            return "trace-fixture"

    class FakeConnection:
        def __init__(self, hostname, port, timeout):
            seen["tls_hostname"] = hostname
            self.timeout = timeout

        def request(self, method, path, body, headers):
            seen.update(path=path, payload=json.loads(body), authorization=headers["Authorization"])
            self.sock = self._create_connection((seen["tls_hostname"], 443), self.timeout, None)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(api, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(api.socket, "create_connection", connect)
    client = api.OpenAICompatibleClient(api.APIConfig(
        base_url="https://model.example/v1", api_key="fixture", model="test", connect_ip="192.0.2.9",
    ))
    response, _ = client._call_sync({"model": "test", "messages": []})
    assert seen["endpoint"] == ("192.0.2.9", 443)
    assert seen["tls_hostname"] == "model.example"
    assert seen["path"] == "/v1/chat/completions"
    assert "connect_ip" not in seen["payload"]
    assert response["_client_transport"]["peer_ip"] == "192.0.2.9"
    assert seen["closed"]


def test_fixed_address_refuses_unencrypted_transport():
    client = api.OpenAICompatibleClient(api.APIConfig(
        base_url="http://model.example/v1", api_key="fixture", model="test", connect_ip="192.0.2.9",
    ))
    with pytest.raises(api.ModelAPIError, match="HTTPS"):
        client._call_sync({})
