from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from agent_cli import main as main_mod
from agent_cli.main import ThreadingHTTPServer, open_store
from agent_cli.store import StoreConnectionError


def _init_paired_store(tmp_path: Path) -> None:
    os.environ["AGENT_HOME"] = str(tmp_path)
    main_mod.main(["init"])
    store = open_store()
    try:
        store.set_meta("hub_url", "https://hub.example")
        store.set_meta("device_token", "fake-token")
    finally:
        store.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _running_dashboard(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Starts cmd_dashboard's real HTTP server on a free port in a daemon thread,
    waits for it to accept connections, and shuts it down cleanly afterward."""
    servers: list[ThreadingHTTPServer] = []

    class _CapturingServer(ThreadingHTTPServer):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(main_mod, "ThreadingHTTPServer", _CapturingServer)

    port = _free_port()
    thread = threading.Thread(target=main_mod.cmd_dashboard, args=(["--port", str(port)],), daemon=True)
    thread.start()
    try:
        for _ in range(50):
            try:
                probe = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
                probe.connect()
                probe.close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("dashboard server did not start in time")
        yield port
    finally:
        for httpd in servers:
            httpd.shutdown()
            httpd.server_close()
        thread.join(timeout=2)


def test_dashboard_post_returns_503_on_store_connection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: apply_control now re-raises StoreConnectionError instead of
    swallowing it into a control-ack (so the --follow reconnect loop can catch it),
    but cmd_dashboard's POST handler is a second, unrelated caller with no retry
    logic of its own - it used to rely on apply_control's old swallow-into-ack
    behavior for a clean error response. Without a dashboard-side catch, the same
    connection-loss error would now escape uncaught and kill the request thread
    silently instead of returning a response."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:
        store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})
    finally:
        store.close()

    def broken_apply_control(store: object, runtime: object, message: dict) -> dict:
        raise StoreConnectionError("server closed the connection unexpectedly")

    monkeypatch.setattr(main_mod, "apply_control", broken_apply_control)

    with _running_dashboard(monkeypatch) as port:
        body = json.dumps({"action": "stop"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST",
            "/api/sessions/s1/control",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()

        assert resp.status == 503, (
            "a lost DB connection must surface as a clean error response, not a dead connection"
        )
        assert payload["ok"] is False


def test_dashboard_post_returns_503_when_the_session_lookup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: do_POST's own store.row("session", sid) / store.device_id()
    calls - run before apply_control is ever reached - had no protection at all
    even after apply_control itself was fixed. A lost DB connection during the
    session-ownership check must surface as a clean error response too, not just
    the apply_control path further down."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:
        store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})
    finally:
        store.close()

    def broken_row(self: object, table: str, row_id: str) -> None:
        raise StoreConnectionError("server closed the connection unexpectedly")

    monkeypatch.setattr(main_mod.Store, "row", broken_row)

    with _running_dashboard(monkeypatch) as port:
        body = json.dumps({"action": "stop"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request(
            "POST",
            "/api/sessions/s1/control",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()

        assert resp.status == 503
        assert payload["ok"] is False


def test_dashboard_get_state_returns_503_on_store_connection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: do_GET's /api/state branch calls store.snapshot()/
    store.device_id() with no protection at all - a lost DB connection there must
    surface as a clean error response too, not an uncaught exception."""
    _init_paired_store(tmp_path)

    def broken_snapshot(self: object) -> None:
        raise StoreConnectionError("server closed the connection unexpectedly")

    monkeypatch.setattr(main_mod.Store, "snapshot", broken_snapshot)

    with _running_dashboard(monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/state")
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()

        assert resp.status == 503
        assert payload["ok"] is False


def test_dashboard_get_state_reconnects_after_transient_store_connection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient postgres blip on /api/state must reconnect once and retry the
    same request, so a long-lived ThreadingHTTPServer recovers instead of sticky
    503s until process restart."""
    _init_paired_store(tmp_path)
    snapshot_calls: list[int] = []
    reconnect_calls: list[int] = []
    real_snapshot = main_mod.Store.snapshot
    real_reconnect = main_mod.Store.reconnect

    def flaky_snapshot(self: object) -> object:
        snapshot_calls.append(1)
        if len(snapshot_calls) == 1:
            raise StoreConnectionError("server closed the connection unexpectedly")
        return real_snapshot(self)

    def counting_reconnect(self: object) -> None:
        reconnect_calls.append(1)
        real_reconnect(self)

    monkeypatch.setattr(main_mod.Store, "snapshot", flaky_snapshot)
    monkeypatch.setattr(main_mod.Store, "reconnect", counting_reconnect)

    with _running_dashboard(monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/state")
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()

        assert resp.status == 200, "transient failure must recover on the same request after reconnect"
        assert "sessions" in payload
        assert reconnect_calls == [1], "store.reconnect() must be called exactly once"
        assert snapshot_calls == [1, 1], "snapshot must be tried once, then retried after reconnect"
