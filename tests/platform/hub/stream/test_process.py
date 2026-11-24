"""U-HUB-stream (hub process): a second hub as its own process (`python -m agent_hub`, real resolvers, system clock)
on the database of the test - an event stored by the test hub reaches a browser stream of the process through the
bus; SIGTERM closes the open stream with `resync {hub_restart}` and the process exits with 0 (the open point of the
M0 acceptance: uvicorn's re-raise gave -15)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid

import httpx

from tests.platform.hub.conftest import HubDatabase, HubHarness, KeyRing, hub_environ
from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import SystemClock, free_port, open_stream


def _start(database: HubDatabase, port: int) -> subprocess.Popen[str]:
    environ = {name: value for name, value in os.environ.items() if not name.startswith("AGENT_HUB_")}
    environ.update(hub_environ(database, AGENT_HUB_PORT=str(port)))
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_hub"], env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=1).status_code == 200:
                return process
        except httpx.HTTPError:
            time.sleep(0.1)
    process.kill()
    raise AssertionError("the hub process did not start: " + process.communicate()[1][-2000:])


def test_hub_process_streams_events_and_stops_cleanly_on_sigterm(
    scene: Scene, hub: HubHarness, migrated_db: HubDatabase
) -> None:
    port = free_port(17)
    clock = SystemClock()
    key = KeyRing(hub.pool, clock).session(uuid.UUID(scene.reader.id))  # type: ignore[arg-type]
    process = _start(migrated_db, port)
    try:
        stream = open_stream(f"http://127.0.0.1:{port}", clock, key, org=scene.org)
        hello = stream.frame(10)
        assert hello is not None
        assert hello.event == "hello"
        response = scene.post(scene.writer, scene.wg_event(scene.writer))
        seq = response.json()["results"][0]["scope_seq"]
        frame = stream.frame(10)
        assert frame is not None
        assert (frame.event, frame.data["scope_seq"]) == ("event", seq)
        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        resync = stream.frame(10)
        assert resync is not None
        assert (resync.event, resync.data) == ("resync", {"reason": "hub_restart"})
        assert stream.ended(10)
        stream.close()
        _, stderr = process.communicate(timeout=20)
        assert time.monotonic() - started < 10
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0
    assert "Application shutdown complete" in stderr
    assert migrated_db.writer_password not in stderr
