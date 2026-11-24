"""U-DBL-model: the OpenRouter key management API of the model double (KOR-50, ADR-081 section 7) - create, list,
read, change and delete keys with the management token, chat requests with a created key (charged, `402` at the
limit, `401` while disabled), the control route and the CLI switch."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.common.client import DoubleClient
from agent_doubles.model.__main__ import main
from agent_doubles.model.access import Access
from agent_doubles.model.keys import KeyRequestError, KeyStore, SubKey
from agent_doubles.model.scenario import parse_scenario
from agent_doubles.model.server import ModelDouble

MANAGEMENT = "double-management-token"  # not a secret: the double only compares it
PLATFORM = "double-platform-token"
AUTH = {"Authorization": f"Bearer {MANAGEMENT}"}
REPLY: dict[str, Any] = {"reply": {"content": [{"type": "text", "text": "ok"}], "cost": 0.25}}
QUESTION = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}
HELLO: dict[str, Any] = {
    "model": "m",
    "max_tokens": 10,
    "stream": True,
    "tools": [{"name": "Read"}],
    "messages": [{"role": "user", "content": "What is 2+2?"}],
}
TOKEN_ENV = "DBL_KEYS_MANAGEMENT_TOKEN"


def started(serve: Any, client_for: Any, *, steps: int = 4, page_size: int = 100) -> tuple[ModelDouble, DoubleClient]:
    scenario = parse_scenario(
        {"name": "keys", "openrouter": {}, "steps": [{"id": f"s{i}", **REPLY} for i in range(steps)]}
    )
    double = ModelDouble(
        scenario,
        access=Access("openrouter", PLATFORM),
        management_token=MANAGEMENT,
        key_page_size=page_size,
    )
    serve(double)
    return double, client_for(double)


def create(client: DoubleClient, name: str, limit: float | None = 1.0) -> tuple[dict[str, Any], str]:
    answer = client.request("POST", "/v1/keys", {"name": name, "limit": limit}, AUTH)
    assert answer.status == 201
    body = answer.json()
    return body["data"], body["key"]


def test_keys_are_created_listed_read_changed_and_deleted(serve: Any, client_for: Any) -> None:
    double, client = started(serve, client_for, page_size=2)
    first, secret = create(client, "dfx-a")
    assert secret.startswith("sk-or-v1-")
    assert first["name"] == "dfx-a"
    assert first["limit"] == 1.0
    assert first["usage"] == 0
    assert (first["disabled"], first["limit_remaining"]) == (False, 1.0)
    create(client, "dfx-b", None)
    third, _ = create(client, "dfx-c")
    names = [k["name"] for k in client.request("GET", "/v1/keys", headers=AUTH).json()["data"]]
    assert names == ["dfx-a", "dfx-b"]
    page = client.request("GET", "/v1/keys?offset=2&include_disabled=true", headers=AUTH).json()["data"]
    assert [k["hash"] for k in page] == [third["hash"]]
    assert client.request("GET", "/v1/keys/?offset=3", headers=AUTH).json() == {"data": []}
    read = client.request("GET", f"/v1/keys/{first['hash']}", headers=AUTH).json()["data"]
    assert read == first
    changed = client.request(
        "PATCH", f"/v1/keys/{first['hash']}", {"limit": 2.5, "disabled": True, "name": "renamed"}, AUTH
    ).json()["data"]
    assert (changed["limit"], changed["disabled"], changed["name"]) == (2.5, True, "renamed")
    unlimited = client.request("PATCH", f"/v1/keys/{first['hash']}", {"limit": None}, AUTH).json()["data"]
    assert (unlimited["limit"], unlimited["limit_remaining"]) == (None, None)
    assert client.request("DELETE", f"/v1/keys/{first['hash']}", headers=AUTH).json() == {"deleted": True}
    for method in ("GET", "DELETE"):
        missing = client.request(method, f"/v1/keys/{first['hash']}", headers=AUTH)
        assert (missing.status, missing.json()["error"]["type"]) == (404, "not_found_error")
    assert client.request("PATCH", f"/v1/keys/{first['hash']}", {"limit": 1}, AUTH).status == 404
    assert [k["name"] for k in client.request("GET", "/_double/keys").json()["keys"]] == ["dfx-b", "dfx-c"]
    assert secret not in json.dumps(client.journal())
    assert MANAGEMENT not in json.dumps(client.journal())
    assert double.state()["failures"] == []
    assert double.state()["requests"] == 0  # management calls are not part of a conversation


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/keys", {"limit": 1}),
        ("POST", "/v1/keys", {"name": "", "limit": 1}),
        ("POST", "/v1/keys", {"name": "x", "limit": -1}),
        ("POST", "/v1/keys", {"name": "x", "limit": True}),
        ("POST", "/v1/keys", {"name": "x", "other": 1}),
        ("POST", "/v1/keys", [1]),
        ("GET", "/v1/keys?offset=-1", None),
        ("PATCH", "/v1/keys/KEY", {"disabled": "yes"}),
        ("PATCH", "/v1/keys/KEY", {"name": 3}),
        ("PATCH", "/v1/keys/KEY", {"other": 1}),
        ("PATCH", "/v1/keys/KEY", "text"),
    ],
)
def test_malformed_management_requests_are_400(
    serve: Any, client_for: Any, method: str, path: str, body: object
) -> None:
    double, client = started(serve, client_for)
    made, _ = create(client, "dfx-x")
    answer = client.request(method, path.replace("KEY", made["hash"]), body, AUTH)
    assert (answer.status, answer.json()["error"]["type"]) == (400, "invalid_request_error")
    assert double.state()["failures"] == []


def test_a_body_the_server_cannot_read_is_400(serve: Any, client_for: Any) -> None:
    double, _ = started(serve, client_for)
    host, port = double.url.removeprefix("http://").rsplit(":", 1)
    head = f"POST /v1/keys HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {MANAGEMENT}\r\nConnection: close\r\n"
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall((head + "Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n").encode())
        answer = b"".join(iter(lambda: sock.recv(65536), b""))
    assert answer.startswith(b"HTTP/1.1 400 ")


def test_management_needs_the_management_token(serve: Any, client_for: Any) -> None:
    """The platform key, a created key, no token or a double without a management token are all refused (401) and
    recorded as failures; unknown methods are 405 failures."""
    double, client = started(serve, client_for)
    _, secret = create(client, "dfx-x")
    for headers in ({}, {"Authorization": f"Bearer {PLATFORM}"}, {"Authorization": f"Bearer {secret}"}):
        answer = client.request("GET", "/v1/keys", headers=headers)
        assert (answer.status, answer.json()["error"]["type"]) == (401, "authentication_error")
    assert double.state()["failures"] == ["key management needs the management key"] * 3
    assert client.request("PUT", "/v1/keys", {}, AUTH).status == 405
    assert client.request("PUT", "/v1/keys/abc", {}, AUTH).status == 405
    assert len(double.state()["failures"]) == 5
    off = serve(
        ModelDouble(
            parse_scenario({"name": "t", "steps": [{"id": "s", "reply": {"content": [{"type": "text", "text": "x"}]}}]})
        )
    )
    assert client_for(off).request("GET", "/v1/keys", headers=AUTH).status == 401
    assert client_for(off).request("GET", "/_double/keys").json() == {"keys": []}


def test_a_created_key_chats_is_charged_and_stops_at_its_limit(serve: Any, client_for: Any) -> None:
    """A created key is an OpenRouter credential: each answer adds its cost to the key's usage; at the limit the
    request is refused with 402, a disabled key with 401 - neither is a failure of the scenario."""
    double, client = started(serve, client_for)
    made, secret = create(client, "dfx-x", 0.5)
    bearer = {"Authorization": f"Bearer {secret}"}
    assert client.request("POST", "/v1/messages", QUESTION, bearer).status == 200
    assert client.request("GET", f"/v1/keys/{made['hash']}", headers=AUTH).json()["data"]["usage"] == 0.25
    assert client.request("POST", "/v1/messages", QUESTION, bearer).status == 200
    refused = client.request("POST", "/v1/messages", QUESTION, bearer)
    assert (refused.status, refused.json()["error"]["type"]) == (402, "payment_required")
    document = client.request("GET", f"/v1/keys/{made['hash']}", headers=AUTH).json()["data"]
    assert (document["usage"], document["limit_remaining"]) == (0.5, 0.0)
    client.request("PATCH", f"/v1/keys/{made['hash']}", {"limit": 1.0}, AUTH)
    assert client.request("POST", "/v1/messages/count_tokens", QUESTION, bearer).status == 200
    client.request("PATCH", f"/v1/keys/{made['hash']}", {"disabled": True}, AUTH)
    disabled = client.request("POST", "/v1/messages", QUESTION, bearer)
    assert (disabled.status, disabled.json()["error"]["message"]) == (401, "key disabled")
    # the platform key still works; a wrong bearer token is the usual failure; an x-api-key never selects a key
    assert client.request("POST", "/v1/messages", QUESTION, {"Authorization": f"Bearer {PLATFORM}"}).status == 200
    assert client.request("POST", "/v1/messages", QUESTION, {"Authorization": "Bearer other"}).status == 401
    assert client.request("POST", "/v1/messages", QUESTION, {**bearer, "x-api-key": "k"}).status == 401
    assert double.state()["failures"] == ["invalid bearer token", "x-api-key header not expected for a bearer token"]
    assert secret not in json.dumps(client.journal())


def test_the_key_store_directly() -> None:
    store = KeyStore()
    assert not store.authorized(f"Bearer {MANAGEMENT}")
    key, secret = store.create({"name": "n"})
    assert key.limit is None
    assert not key.exhausted
    assert "redacted" in repr(key)
    assert secret not in repr(key)
    assert store.by_bearer("Basic x") is None
    assert store.by_bearer(f"Bearer {secret}") is key
    assert store.patch("missing", {"limit": 1}) is None
    assert not store.delete("missing")
    with pytest.raises(KeyRequestError):
        store.create({"name": "n", "limit": "1"})
    assert isinstance(key, SubKey)


def test_cli_switches_the_management_api_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TOKEN_ENV, MANAGEMENT)
    port_file = tmp_path / "port"
    seen: dict[str, int] = {}

    def drive(stop: threading.Event) -> None:
        client = DoubleClient(f"http://127.0.0.1:{port_file.read_text().strip()}")
        seen["created"] = client.request("POST", "/v1/keys", {"name": "n", "limit": 1}, AUTH).status
        client.request("POST", "/v1/messages", HELLO)

    code = main(["--scenario", "hello", "--port-file", str(port_file), "--management-token-env", TOKEN_ENV], wait=drive)
    assert code == 0
    assert seen["created"] == 201
    capsys.readouterr()
    monkeypatch.delenv(TOKEN_ENV)
    assert main(["--scenario", "hello", "--management-token-env", TOKEN_ENV]) == 2
    assert "must not be empty" in capsys.readouterr().err
