"""U-HUB-vnc (routes): `GET /v1/vnc/{request}` and `GET /v1/device/vnc/{request}` on a real uvicorn server with
`websockets` clients (ADR-101 section 3): the owner's own identity and the addressed device are paired and their
records relayed unchanged; another user, a request the user's agent sent, another device, another kind, a follow-up,
a closed or unknown request are refused before anything is relayed; the audit rows carry no content."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect

from agent_hub.crypto.ids import uuid7
from tests.platform.hub.conftest import HubHarness, SigningKey, contracts_registry
from tests.platform.hub.credentials.support import Desk
from tests.platform.hub.orgs.support import Actor, World
from tests.platform.hub.stream.support import LiveHub, signed_headers

PAIRED = '{"type":"paired"}'


@pytest.fixture
def desk(world: World) -> Desk:
    return Desk.build(world)


def _open(desk: Desk, user: Actor, **options: Any) -> str:
    body = {"kind": "login", "id": uuid7()}
    envelope = desk.request(user, desk.scene.device, "vnc.open", body=body, **options)
    response = desk.send(user, envelope)
    assert response.status_code == 200, response.text
    return str(envelope["header"]["id"])


def _connect(live: LiveHub, key: SigningKey, path: str) -> ClientConnection:
    opener = ws_connect(
        f"ws://127.0.0.1:{live.port}{path}",
        additional_headers=signed_headers(key, live.clock, path),
        open_timeout=10,
        max_size=2**20,
    )
    socket = opener.__enter__()
    live.opened.append(socket.close)
    return socket


def _device_key(desk: Desk, device: Actor) -> SigningKey:
    key = desk.client(device).key
    assert key is not None
    return key


def _user_key(user: Actor) -> SigningKey:
    key = user.browser.key
    assert key is not None
    return key


def _audit(hub: HubHarness, request_id: str) -> list[tuple[str, str, dict[str, Any]]]:
    with hub.pool.connection() as connection:
        rows = connection.execute(
            "SELECT action, actor_kind, detail FROM hub.audit WHERE target_id = %s AND action LIKE 'vnc.%%'"
            " ORDER BY seq",
            (request_id,),
        ).fetchall()
    for action, _actor, detail in rows:
        contracts_registry().validate(f"audit/{action}.json", detail)
    return [(str(action), str(actor), dict(detail)) for action, actor, detail in rows]


def test_the_owner_and_the_device_are_paired_and_records_pass_unchanged(
    hub: HubHarness, live: LiveHub, desk: Desk
) -> None:
    writer, device = desk.scene.writer, desk.scene.device
    request_id = _open(desk, writer)
    device_end = _connect(live, _device_key(desk, device), f"/v1/device/vnc/{request_id}")
    client_end = _connect(live, _user_key(writer), f"/v1/vnc/{request_id}")
    assert device_end.recv(timeout=10) == PAIRED
    assert client_end.recv(timeout=10) == PAIRED
    device_end.send(b"sealed-record-from-the-device")
    assert client_end.recv(timeout=10) == b"sealed-record-from-the-device"
    client_end.send(b"sealed-record-from-the-client")
    assert device_end.recv(timeout=10) == b"sealed-record-from-the-client"
    client_end.close()
    with pytest.raises(ConnectionClosed):
        device_end.recv(timeout=10)
    rows = _audit(hub, request_id)
    for _ in range(200):
        if len(rows) == 2:
            break
        rows = _audit(hub, request_id)
    assert [(action, actor) for action, actor, _ in rows] == [("vnc.connect", "user"), ("vnc.close", "user")]
    assert rows[1][2]["reason"] == "client_left"
    assert rows[1][2]["bytes"] == len(b"sealed-record-from-the-device") + len(b"sealed-record-from-the-client")
    text = json.dumps([detail for _, _, detail in rows])
    assert "sealed-record" not in text


def _refused(live: LiveHub, key: SigningKey, path: str) -> None:
    with pytest.raises(InvalidStatus) as refused:
        _connect(live, key, path)
    assert refused.value.response.status_code == 403


def test_only_the_owners_own_identity_and_the_addressed_device_join(hub: HubHarness, live: LiveHub, desk: Desk) -> None:
    writer, reader, device = desk.scene.writer, desk.scene.reader, desk.scene.device
    request_id = _open(desk, writer)
    _refused(live, _user_key(reader), f"/v1/vnc/{request_id}")  # another user
    _refused(live, _device_key(desk, desk.scene.other_device), f"/v1/device/vnc/{request_id}")  # another device
    _refused(live, _user_key(writer), "/v1/vnc/not-a-request")
    _refused(live, _user_key(writer), f"/v1/vnc/{uuid.UUID(int=7)}")
    login = desk.request(writer, device, "claude.login", body={"mode": "manual"})
    assert desk.send(writer, login).status_code == 200
    _refused(live, _user_key(writer), f"/v1/vnc/{login['header']['id']}")  # another kind
    follow = desk.request(writer, device, "vnc.open", follows=request_id, body={"kind": "login", "id": uuid7()})
    assert desk.send(writer, follow).status_code == 200
    _refused(live, _user_key(writer), f"/v1/vnc/{follow['header']['id']}")  # a follow-up
    sent_by_agent = _open(desk, writer)
    with hub.pool.connection() as connection:
        # the hub stores an agent's request under its user with the agent as the envelope's sender (ADR-082)
        connection.execute(
            "UPDATE hub.device_request SET envelope = jsonb_set(envelope::jsonb, '{header,user}', to_jsonb(%s::text))"
            "::text WHERE id = %s",
            (str(uuid7()), sent_by_agent),
        )
        connection.execute("UPDATE hub.device_request SET state = 'answered' WHERE id = %s", (request_id,))
    _refused(live, _user_key(writer), f"/v1/vnc/{sent_by_agent}")  # never an agent
    _refused(live, _user_key(writer), f"/v1/vnc/{request_id}")  # closed
    _refused(live, _device_key(desk, device), f"/v1/device/vnc/{request_id}")
    assert _audit(hub, request_id) == []
