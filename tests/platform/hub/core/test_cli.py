"""The main command `agent-hub` / `python -m agent_hub`: discovered subcommands, `serve`, configuration errors."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any

import httpx
import pytest
import uvicorn

from agent_hub import app as app_module
from agent_hub import cli
from tests.platform.hub.conftest import TEST_HUB_SEED, HubDatabase, hub_environ

HUB_VARIABLES = ("AGENT_HUB_",)


def free_port(offset: int) -> int:
    """A port of this work package's range when it is set, else a free ephemeral port."""
    base = os.environ.get("WP_PORT_BASE")
    if base:
        return int(base) + offset
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def clean_environ(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("AGENT_HUB_") and name != "AGENT_HUB_TEST_POSTGRES":
            monkeypatch.delenv(name)
    return monkeypatch


def set_environ(monkeypatch: pytest.MonkeyPatch, environ: dict[str, str]) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


def test_subcommands_are_discovered() -> None:
    parser = cli.build_parser()
    help_text = parser.format_help()
    assert "serve" in help_text
    assert "migrate" in help_text


def test_configuration_error(clean_environ: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    set_environ(clean_environ, hub_environ(AGENT_HUB_SIGNING_KEY="ZZ" + TEST_HUB_SEED[2:]))
    assert cli.main(["serve"]) == cli.EXIT_CONFIG
    error = capsys.readouterr().err
    assert error.startswith("agent-hub: AGENT_HUB_SIGNING_KEY must be")
    assert TEST_HUB_SEED[2:] not in error


def test_owner_database_stops_the_start(clean_environ: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    set_environ(clean_environ, hub_environ(AGENT_HUB_OWNER_DATABASE="postgresql://x"))
    assert cli.main([]) == cli.EXIT_CONFIG
    assert "AGENT_HUB_OWNER_DATABASE must not be set" in capsys.readouterr().err


def test_migrate_is_a_discovered_subcommand(
    clean_environ: pytest.MonkeyPatch, migrated_db: HubDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    set_environ(clean_environ, migrated_db.migrate_env())
    assert cli.main(["migrate"]) == 0
    assert capsys.readouterr().out.startswith("unchanged ")


def run_in_thread(argv: list[str]) -> tuple[threading.Thread, list[Any]]:
    result: list[Any] = []

    def target() -> None:
        try:
            result.append(cli.main(argv))
        except BaseException as error:  # SystemExit from uvicorn
            result.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


IDLE_PROBE_S = 5.5
"""Longer than uvicorn's default idle limit (5 s) that caused the keep-alive race of KOR-18."""


def answered(data: bytes) -> bool:
    """True when `data` holds a complete answer (head and `content-length` bytes of body)."""
    head, found, rest = data.partition(b"\r\n\r\n")
    if not found:
        return False
    lengths = [line.split(b":")[1] for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")]
    return len(rest) >= int(lengths[0])


def assert_idle_connection_kept(port: int) -> None:
    """Two requests on one connection with an idle pause longer than 5 s: the second is answered."""
    request = b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        for pause in (0.0, IDLE_PROBE_S):
            time.sleep(pause)
            conn.sendall(request)
            answer = b""
            while not answered(answer):
                chunk = conn.recv(65536)
                assert chunk, "the hub closed the idle connection"
                answer += chunk
            assert answer.startswith(b"HTTP/1.1 200")


def test_serve_in_process(
    clean_environ: pytest.MonkeyPatch, migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = free_port(10)
    set_environ(clean_environ, hub_environ(migrated_db, AGENT_HUB_PORT=str(port)))
    servers: list[uvicorn.Server] = []
    monkeypatch.setattr(cli, "SERVER_OBSERVERS", [servers.append])
    thread, result = run_in_thread(["serve"])
    deadline = time.monotonic() + 15
    body: dict[str, Any] = {}
    while time.monotonic() < deadline and not body:
        try:
            body = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1).json()
        except httpx.HTTPError:
            time.sleep(0.05)
    assert body.get("ok") is True
    response = httpx.get(f"http://127.0.0.1:{port}/v1/config", timeout=10)
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert "server" not in response.headers
    assert servers[0].config.timeout_keep_alive == cli.KEEP_ALIVE_S
    assert_idle_connection_kept(port)
    servers[0].should_exit = True
    thread.join(15)
    assert result == [0]


def test_serve_fails_without_a_database(
    clean_environ: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    unreachable = free_port(11)
    url = f"postgresql://writer:{'0' * 64}@127.0.0.1:{unreachable}/hub?sslmode=disable"
    set_environ(clean_environ, hub_environ(AGENT_HUB_DATABASE=url, AGENT_HUB_PORT=str(free_port(12))))
    monkeypatch.setattr(app_module, "POOL_OPEN_TIMEOUT_SECONDS", 0.5)
    thread, result = run_in_thread([])
    thread.join(30)
    [stop] = result
    assert isinstance(stop, SystemExit)
    assert stop.code == cli.EXIT_START_FAILED
    assert "0" * 64 not in caplog.text


def test_python_m_agent_hub_starts_and_stops(migrated_db: HubDatabase) -> None:
    port = free_port(13)
    environ = {name: value for name, value in os.environ.items() if not name.startswith("AGENT_HUB_")}
    environ.update(hub_environ(migrated_db, AGENT_HUB_PORT=str(port)))
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub"], env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 20
        ok = False
        while time.monotonic() < deadline and not ok:
            try:
                ok = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                time.sleep(0.1)
        assert ok
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    # An orderly shutdown exits with 0 (no re-raise of SIGTERM, ACC-M0 open point).
    assert process.returncode == 0
    assert "Application shutdown complete" in stderr
    assert TEST_HUB_SEED not in stderr
    assert migrated_db.writer_password not in stderr
