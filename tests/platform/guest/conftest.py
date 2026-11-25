"""Shared fixtures of the guest suites (GST-01, ADR-027 section 6): temporary socket paths, a journal in tmp, a fake
clock, and test clients for the control socket, ``api.sock`` and the API port. Area suites add
``tests/platform/guest/<area>/conftest.py``.

Socket paths live in a short directory under ``/tmp`` because ``AF_UNIX`` paths are limited to about 104 bytes on
macOS; pytest's ``tmp_path`` is too long there.
"""

from __future__ import annotations

import contextlib
import grp
import http.client
import json
import os
import shutil
import socket
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest.apisock import ApiClient, token_sha256
from agent_guest.config import GuestConfig, load_config
from agent_guest.control import ControlContext, ControlService
from agent_guest.journal import Journal
from agent_guest.schema import SchemaSet
from agent_guest.server import ApiHttpServer, create_server
from agent_guest.state import Session

CONTROL_TOKEN = "Y29udHJvbC10b2tlbi1mb3ItdGVzdHMtb25seS0xMjM0"
BOOT_ID = "0b6f7f2c-8d1e-4f3a-9c5b-7a6e5d4c3b2a"
START = 1_790_000_000.0
SESSION_ID = "01a1ee56-5e00-7003-8f1b-340691d99f03"
SESSION_TOKEN = "c2Vzc2lvbi10b2tlbi1mb3ItdGVzdHMtb25seS01Njc4"


class FakeClock:
    """``now()`` returns a fixed time that tests move with ``advance``."""

    def __init__(self, start: float = START) -> None:
        self.value = start

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass
class Reply:
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


class ControlClient:
    """HTTP/1.1 client of the control socket with the bearer token (``token=None`` sends no header)."""

    def __init__(self, path: str, token: str | None = CONTROL_TOKEN) -> None:
        self.path = path
        self.token = token

    def request(
        self,
        method: str,
        target: str,
        body: object = None,
        *,
        raw: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        token: str | None = "",
    ) -> Reply:
        sent: dict[str, str] = dict(headers or {})
        use = self.token if token == "" else token
        if use is not None:
            sent["Authorization"] = f"Bearer {use}"
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if data is not None:
            sent.setdefault("Content-Type", "application/json")
        connection = UnixHTTPConnection(self.path)
        try:
            # http.client sends the head and the body separately; a server that answers before it reads the body
            # (404 for an unknown path) and closes can meet the second send under load. Its answer is there.
            with contextlib.suppress(BrokenPipeError):
                connection.request(method, target, body=data, headers=sent)
            response = connection.getresponse()
            return Reply(response.status, response.headers, response.read())
        finally:
            connection.close()


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="gst", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(scope="session")
def contracts_dir(repo_root: Path) -> Path:
    return repo_root / "contracts"


@pytest.fixture(scope="session")
def schemas(contracts_dir: Path) -> SchemaSet:
    return SchemaSet(contracts_dir)


@pytest.fixture
def own_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


@pytest.fixture
def guest_env(short_tmp: Path, tmp_path: Path, contracts_dir: Path, own_group: str) -> dict[str, str]:
    """``AGENT_GUEST_*`` of a guest that runs as the test user in temporary directories."""
    token = tmp_path / "control-token"
    token.write_text(CONTROL_TOKEN + "\n", encoding="ascii")
    boot = tmp_path / "boot_id"
    boot.write_text(BOOT_ID + "\n", encoding="ascii")
    build = tmp_path / "build.json"
    build.write_text(json.dumps({"landlock_abi": 6}), encoding="utf-8")
    install = tmp_path / "install.json"
    install.write_text(json.dumps({"version": "2.1.330"}), encoding="utf-8")
    return {
        "AGENT_GUEST_CONTROL_SOCKET": str(short_tmp / "control.sock"),
        "AGENT_GUEST_API_SOCKET": str(short_tmp / "api.sock"),
        "AGENT_GUEST_CONTROL_GROUP": own_group,
        "AGENT_GUEST_API_GROUP": own_group,
        "AGENT_GUEST_CONTROL_TOKEN_FILE": str(token),
        "AGENT_GUEST_JOURNAL": str(tmp_path / "state" / "journal.db"),
        "AGENT_GUEST_API_PORT": "0",
        "AGENT_GUEST_BUILD_FILE": str(build),
        "AGENT_GUEST_CLAUDE_INSTALL_FILE": str(install),
        "AGENT_GUEST_BOOT_ID_FILE": str(boot),
        "AGENT_GUEST_CONTRACTS": str(contracts_dir),
        "AGENT_GUEST_ORCH_SOCKET": str(short_tmp / "orch.sock"),
        "AGENT_GUEST_ORCH_GROUP": own_group,
        "AGENT_GUEST_ORCH_MCP_SOCKET": str(short_tmp / "orch-mcp.sock"),
    }


@pytest.fixture
def guest_config(guest_env: dict[str, str]) -> GuestConfig:
    return load_config(guest_env)


@pytest.fixture
def journal(tmp_path: Path, clock: FakeClock, schemas: SchemaSet) -> Iterator[Journal]:
    opened = Journal.open(str(tmp_path / "journal" / "journal.db"), clock, schemas)
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def control(guest_config: GuestConfig, clock: FakeClock) -> Iterator[ControlService]:
    """A started control process (both sockets, journal) inside the test process."""
    service = ControlService(guest_config, clock)
    service.start()
    try:
        yield service
    finally:
        service.stop()


@pytest.fixture
def context(control: ControlService) -> ControlContext:
    assert control.context is not None
    return control.context


@pytest.fixture
def control_client(guest_config: GuestConfig, control: ControlService) -> ControlClient:
    return ControlClient(guest_config.control_socket)


@pytest.fixture
def api_client(guest_config: GuestConfig, control: ControlService) -> Iterator[ApiClient]:
    client = ApiClient(guest_config.api_socket, timeout=10.0)
    try:
        yield client
    finally:
        client.close()


def make_session(session_id: str = SESSION_ID, token: str = SESSION_TOKEN, state: str = "working") -> Session:
    return Session(
        id=session_id,
        uid=20000,
        token_sha256=token_sha256(token),
        state=state,
        workspace=f"/srv/sessions/{session_id}/work",
        claude_session_id=None,
        created_at="2026-11-21T10:00:00.000Z",
    )


@pytest.fixture
def session(context: ControlContext) -> Session:
    """One working session in the control process's cache and database."""
    created = make_session()
    context.state.save_session(created)
    return created


@pytest.fixture
def api_server(guest_config: GuestConfig, control: ControlService) -> Iterator[ApiHttpServer]:
    """The API process's HTTP server on 127.0.0.1 with a free port, talking to the control fixture."""
    server = create_server(replace(guest_config, api_port=0))
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        server.context.control.close()
        thread.join(5)


@pytest.fixture
def api_port(api_server: ApiHttpServer) -> int:
    return int(api_server.server_address[1])
