from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent_cli import main as main_mod
from agent_cli.main import open_store
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

    port = 18745
    thread = threading.Thread(target=main_mod.cmd_dashboard, args=(["--port", str(port)],), daemon=True)
    thread.start()

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

    assert resp.status == 503, "a lost DB connection must surface as a clean error response, not a dead connection"
    assert payload["ok"] is False
