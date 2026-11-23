"""Session tokens bound to the connecting UID; failed checks counted, limited and journaled (ATK-36, ATS-02).

Units: ``peer.py`` (``/proc/net/tcp`` parsing), ``authguard.py`` (failure window), the control op
``session.resolve``; end to end through the API port with a socket table written by the test for the test client's
own connection (``/mcp``, ``/hooks/<Event>``, ``/model/…``).
"""

from __future__ import annotations

import http.client
import json
import socket
import struct
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest import peer
from agent_guest.apisock import ApiClient, ApiSockError, AuthLimited
from agent_guest.authguard import AuthGuard, Failure
from agent_guest.config import ConfigError, GuestConfig, load_config
from agent_guest.control import ControlContext, ControlService
from agent_guest.routing import _tcp
from agent_guest.server import ApiHttpServer, create_server
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, make_session

HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
OTHER_ID = "01a1ee56-5e00-7003-8f1b-340691d99f04"
OTHER_TOKEN = "b3RoZXItc2Vzc2lvbi10b2tlbi1mb3ItdGVzdHMtMDAx"


def hexaddr(host: str, port: int) -> str:
    number = struct.unpack("<I", socket.inet_aton(host))[0]
    return f"{number:08X}:{port:04X}"


def line(local: tuple[str, int], remote: tuple[str, int], uid: int | str, state: str = "01") -> str:
    return f"   0: {hexaddr(*local)} {hexaddr(*remote)} {state} 00000000:00000000 00:00000000 00000000 {uid} 0 1 1\n"


# ---------------------------------------------------------------- peer.py


def test_parse_table_keeps_live_client_connections_only() -> None:
    a, b = ("127.0.0.1", 40000), ("127.0.0.1", 7700)
    text = (
        HEADER
        + line(a, b, 20001)
        + line(b, a, 999)
        + line(("127.0.0.1", 40001), b, 20002, state="0A")
        + line(("127.0.0.1", 40003), b, 20004, state="05")
        + line(("127.0.0.1", 40004), b, 0, state="06")
        + line(("127.0.0.1", 40002), b, "x")
        + "   1: short line\n"
        + "   2: 0100007F 0100007F:1E14 01 0 0 0 20003 0 1\n"
        + "   3: 0100007F:1E14:00 0100007F:1E14 01 0 0 0 20003 0 1\n"
        + "   4: ZZZZZZZZ:1E14 0100007F:1E14 01 0 0 0 20003 0 1\n"
        + "   5: 0100007F:1E14 0100007F:ZZZZ 01 0 0 0 20003 0 1\n"
    )
    assert peer.parse_table(text) == {(a, b): 20001, (b, a): 999, (("127.0.0.1", 40003), b): 20004}


def test_peer_uid(tmp_path: Path) -> None:
    server, client = ("127.0.0.1", 7700), ("127.0.0.1", 40000)
    table = tmp_path / "tcp"
    table.write_text(HEADER + line(client, server, 20005))
    assert peer.peer_uid("", server, client) is None
    assert peer.peer_uid(str(table), server, client) == 20005
    assert peer.peer_uid(str(table), server, ("127.0.0.1", 40001)) == peer.UNKNOWN
    assert peer.peer_uid(str(table), None, client) == peer.UNKNOWN
    assert peer.peer_uid(str(table), server, None) == peer.UNKNOWN
    assert peer.peer_uid(str(tmp_path / "missing"), server, client) == peer.UNKNOWN


def test_tcp_address_of_a_request() -> None:
    assert _tcp(("127.0.0.1", 5, 0, 0)) == ("127.0.0.1", 5)
    assert _tcp("/run/agent-guest/control.sock") is None
    assert _tcp(("127.0.0.1",)) is None
    assert _tcp(("127.0.0.1", "5")) is None
    assert _tcp((1, 5)) is None


def test_peer_table_configuration(guest_env: dict[str, str]) -> None:
    assert load_config(guest_env).peer_table == ""
    assert load_config({**guest_env, "AGENT_GUEST_PEER_TABLE": "/proc/net/tcp"}).peer_table == "/proc/net/tcp"
    for value in ("proc/net/tcp", "/proc/net/tcp\n", "/a\x00b"):
        with pytest.raises(ConfigError) as error:
            load_config({**guest_env, "AGENT_GUEST_PEER_TABLE": value})
        assert error.value.name == "PEER_TABLE"


# ---------------------------------------------------------------- authguard.py


class Ticks:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_guard_limits_per_key_and_records_the_start_once() -> None:
    ticks = Ticks()
    guard = AuthGuard(ticks, limit=3, window=60.0)
    assert [guard.fail(7) for _ in range(3)] == [Failure("auth_failed", None)] * 3
    ticks.now += 10
    assert guard.fail(7) == Failure("auth_rate_limited", 50)
    assert guard.fail(7) == Failure(None, 50)
    assert guard.fail(8) == Failure("auth_failed", None)  # another UID is not affected
    ticks.now += 49.5
    assert guard.fail(7) == Failure(None, 1)  # at least one second
    ticks.now += 0.5
    assert guard.fail(7) == Failure("auth_failed", None)  # the window has room again
    ticks.now += 0.1
    assert [guard.fail(7) for _ in range(2)] == [Failure("auth_failed", None)] * 2
    assert guard.fail(7) == Failure("auth_rate_limited", 60)  # a new limit is journaled again


def test_guard_forgets_the_oldest_key() -> None:
    ticks = Ticks()
    guard = AuthGuard(ticks, limit=1, window=60.0, max_keys=2)
    guard.fail(1)
    assert guard.fail(1) == Failure("auth_rate_limited", 60)
    guard.fail(2)
    guard.fail(None)  # third key: UID 1 is dropped together with its limit
    assert guard.fail(1) == Failure("auth_failed", None)
    assert guard.fail(None) == Failure("auth_rate_limited", 60)


# ---------------------------------------------------------------- session.resolve


def guest_errors(context: ControlContext) -> list[tuple[str | None, str]]:
    with context.journal.transaction() as db:
        rows = db.execute("SELECT session_id, body FROM journal WHERE kind = 'guest.error' ORDER BY seq").fetchall()
    return [(row[0], json.loads(row[1])["code"]) for row in rows]


def test_resolve_binds_the_token_to_the_session_uid(
    api_client: ApiClient, context: ControlContext, session: Session
) -> None:
    context.state.save_session(replace(make_session(OTHER_ID, OTHER_TOKEN), uid=20001))
    assert api_client.resolve_session(SESSION_TOKEN, 20000) is not None
    assert api_client.resolve_session(SESSION_TOKEN, None) is not None
    assert api_client.resolve_session(SESSION_TOKEN, 20001) is None  # foreign token
    assert api_client.resolve_session(SESSION_TOKEN, peer.UNKNOWN) is None
    assert api_client.resolve_session(None, 20000) is None
    assert guest_errors(context) == [(OTHER_ID, "auth_failed"), (None, "auth_failed"), (SESSION_ID, "auth_failed")]
    for args in ({"token_sha256": "x", "peer_uid": "20000"}, {"token_sha256": "x", "peer_uid": True}):
        with pytest.raises(ApiSockError) as error:
            api_client.call("session.resolve", args)
        assert error.value.code == "request_invalid"


def test_resolve_limits_failures_and_never_the_valid_token(
    api_client: ApiClient, context: ControlContext, session: Session
) -> None:
    context.guard = AuthGuard(lambda: 5.0, limit=2, window=30.0)
    context.state.save_session(replace(make_session(OTHER_ID, OTHER_TOKEN), uid=20001, state="stopped"))
    for _ in range(2):
        assert api_client.resolve_session("guess", 20001) is None
    with pytest.raises(AuthLimited) as limited:
        api_client.resolve_session("guess", 20001)
    assert limited.value.retry_after == 30
    with pytest.raises(AuthLimited):
        api_client.resolve_session(SESSION_TOKEN, 20001)  # a foreign token is a failure as well
    assert api_client.resolve_session(SESSION_TOKEN, 20000) is not None  # the owner is never limited
    assert guest_errors(context) == [(None, "auth_failed"), (None, "auth_failed"), (None, "auth_rate_limited")]


# ---------------------------------------------------------------- end to end through the API port


@pytest.fixture
def bound_server(guest_config: GuestConfig, control: ControlService, tmp_path: Path) -> Iterator[ApiHttpServer]:
    table = tmp_path / "tcp"
    table.write_text(HEADER)
    server = create_server(replace(guest_config, api_port=0, peer_table=str(table)))
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.context.control.close()
        thread.join(5)


def call(
    server: ApiHttpServer, uid: int | None, path: str, token: str | None, body: Any = None, *, api_key: bool = False
) -> tuple[int, dict[str, str], Any]:
    """One request whose client socket the table assigns to `uid` (``None``: not listed)."""
    port = int(server.server_address[1])
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.connect()
    assert connection.sock is not None
    client = connection.sock.getsockname()[:2]
    table = Path(server.context.config.peer_table)
    table.write_text(HEADER + (line(client, ("127.0.0.1", port), uid) if uid is not None else ""))
    data = json.dumps(body if body is not None else {}).encode()
    headers = {"Content-Type": "application/json", "Content-Length": str(len(data))}
    if token is not None:
        headers["x-api-key" if api_key else "Authorization"] = token if api_key else f"Bearer {token}"
    connection.request("POST", path, body=data, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(raw) if raw else None


PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


def test_api_accepts_the_owner_and_refuses_foreign_uids(
    bound_server: ApiHttpServer, context: ControlContext, session: Session
) -> None:
    assert call(bound_server, 20000, "/mcp", SESSION_TOKEN, PING)[0] == 200
    assert call(bound_server, 20001, "/mcp", SESSION_TOKEN, PING)[0] == 401
    assert call(bound_server, None, "/mcp", SESSION_TOKEN, PING)[0] == 401
    status, _, body = call(bound_server, 20001, "/model/v1/messages", SESSION_TOKEN, {"model": "m"}, api_key=True)
    assert (status, body["error"]["type"]) == (401, "authentication_error")
    assert call(bound_server, 20001, "/hooks/Stop", SESSION_TOKEN, {})[0] == 401
    assert [code for _, code in guest_errors(context)] == ["auth_failed"] * 4


def test_api_rate_limits_failed_checks(bound_server: ApiHttpServer, context: ControlContext, session: Session) -> None:
    context.guard = AuthGuard(lambda: 1.0, limit=1, window=20.0)
    assert call(bound_server, 20001, "/hooks/PreToolUse", "guess", {})[0] == 401
    for path, body in (("/mcp", PING), ("/hooks/PreToolUse", {}), ("/hooks/Stop", None)):
        status, headers, answer = call(bound_server, 20001, path, None if body is None else "guess", body)
        assert (status, headers["retry-after"], answer) == (429, "20", {"error": "rate_limited"})
    for path in ("/model/v1/messages", "/model/v1/other"):
        status, headers, answer = call(bound_server, 20001, path, "guess", {"model": "m"})
        assert (status, headers["retry-after"], answer["error"]["type"]) == (429, "20", "rate_limit_error")
    assert call(bound_server, 20000, "/mcp", SESSION_TOKEN, PING)[0] == 200
    assert [code for _, code in guest_errors(context)] == ["auth_failed", "auth_rate_limited"]
