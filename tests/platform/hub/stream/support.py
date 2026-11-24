"""Test support of the live-channel suites (U-HUB-stream): a hub app served by a real uvicorn server, a signed
streaming SSE client and a signed `websockets` client, both validating every frame against `contracts/ws/`.

The org, its actors and its events come from the events suites (`Scene`), built through the TestClient harness on the
same database; the live app shares the harness's key ring, clock and pool. The harness app's own live service is
detached from the process publisher, so a frame published in this process reaches only the live app (as in
production, where one app runs per process).
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI
from websockets.sync.client import ClientConnection
from websockets.sync.client import connect as ws_connect

from agent_core.signing import b64url_encode, format_time, request_text, sha256_hex
from agent_hub import signals
from agent_hub.app import create_app
from agent_hub.auth.keys import DEVICE_PREFIX, Principal, ed25519_verifier
from agent_hub.cli import HubServer
from agent_hub.config import HubConfig
from agent_hub.crypto.keys import Identity
from agent_hub.stream.publish import LIVE
from agent_hub.stream.service import StreamService, service_of
from tests.platform.hub.conftest import FixedClock, HubHarness, KeyRing, SigningKey, contracts_registry
from tests.platform.hub.orgs.support import Actor

PUBLIC_HOST = "localhost:8788"
# Default wait of the stream helpers for a frame, a hint or a condition that must come (cross-thread delivery through
# uvicorn and PostgreSQL LISTEN/NOTIFY); only a failing test waits it out. Negative checks use `quiet()` (FIX-01).
WAIT_S = 30.0
CAPACITY = {
    "vm_memory_budget_mib": 8192,
    "vm_memory_used_mib": 0,
    "vm_slots_total": 4,
    "vm_slots_free": 4,
    "pool_ready": 1,
}


def free_port(offset: int) -> int:
    """A port of this work package's range when it is set, else a free ephemeral port."""
    base = os.environ.get("WP_PORT_BASE")
    if base:
        return int(base) + offset
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class SystemClock:
    """The real time, for a hub process that runs with the system clock."""

    def __call__(self) -> datetime:
        return datetime.now(UTC)


def signed_headers(key: SigningKey, clock: Callable[[], datetime], target: str, method: str = "GET") -> dict[str, str]:
    """The `agent-request-v1` headers of a request without a body."""
    agent_time = format_time(int(clock().timestamp()))
    nonce = b64url_encode(secrets.token_bytes(16))
    text = request_text(key.key_id, method, PUBLIC_HOST, target, agent_time, nonce, sha256_hex(b""))
    return {
        "Agent-Key": key.key_id,
        "Agent-Time": agent_time,
        "Agent-Nonce": nonce,
        "Agent-Signature": b64url_encode(key.sign(text)),
    }


def browser_key(actor: Actor) -> SigningKey:
    """The browser session key of a scene user."""
    key = actor.browser.key
    assert key is not None
    return key


def device_key(keys: KeyRing, actor: Actor, org: str) -> SigningKey:
    """Register the key of a scene device (its identity) with the in-memory resolver and make the scene's org its
    owning org (the fixture inserts org devices with an unrelated owner)."""
    with keys._pool.connection() as connection:
        connection.execute("UPDATE hub.device SET owner_org = %s WHERE id = %s", (org, actor.id))
    device = uuid.UUID(actor.id)
    identity: Identity = actor.identity
    principal = Principal(
        kind="device",
        key_id=DEVICE_PREFIX + device.hex,
        principal_id=device,
        actor_kind="device",
        attributes={"state": "active"},
    )
    key = SigningKey(key_id=principal.key_id or "", sign=identity.sig.sign, principal=principal)
    return keys._remember(key, ed25519_verifier(identity.sig_public()))


@dataclass
class Frame:
    """One SSE item: a frame (`event`, parsed `data`) or a heartbeat comment (`event == ":"`)."""

    event: str
    data: Any = None
    at: float = field(default_factory=time.perf_counter)


class SseClient:
    """A streaming reader of `GET /v1/stream` in a background thread."""

    def __init__(self, response: httpx.Response, client: httpx.Client) -> None:
        self.response = response
        self._client = client
        self.items: queue.Queue[Frame | None] = queue.Queue()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        event, data = "", []
        try:
            for line in self.response.iter_lines():
                if line.startswith(":"):
                    self.items.put(Frame(":"))
                elif line.startswith("event: "):
                    event = line[len("event: ") :]
                elif line.startswith("data: "):
                    data.append(line[len("data: ") :])
                elif line == "" and data:
                    frame = Frame(event, json.loads("\n".join(data)))
                    contracts_registry().validate("ws/sse/frame.json", {"event": frame.event, "data": frame.data})
                    self.items.put(frame)
                    event, data = "", []
        except (httpx.HTTPError, RuntimeError):
            pass
        finally:
            self.items.put(None)

    def next(self, timeout: float = WAIT_S) -> Frame | None:
        """The next item (None when the stream ended)."""
        return self.items.get(timeout=timeout)

    def frame(self, timeout: float = WAIT_S, *, skip_heartbeats: bool = True) -> Frame | None:
        """The next frame, heartbeats skipped."""
        deadline = time.monotonic() + timeout
        while True:
            item = self.next(max(0.01, deadline - time.monotonic()))
            if item is None or item.event != ":" or not skip_heartbeats:
                return item

    def frames(self, count: int, timeout: float = WAIT_S) -> list[Frame]:
        found: list[Frame] = []
        while len(found) < count:
            item = self.frame(timeout)
            assert item is not None, f"stream ended after {found}"
            found.append(item)
        return found

    def quiet(self, seconds: float = 0.3) -> list[Frame]:
        """Every item that arrives within `seconds` (expected: none)."""
        found: list[Frame] = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                item = self.items.get(timeout=left)
            except queue.Empty:
                break
            if item is not None and item.event != ":":
                found.append(item)
        return found

    def ended(self, timeout: float = WAIT_S) -> bool:
        """True when the stream ends within `timeout` (frames before the end are dropped)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.items.get(timeout=max(0.01, deadline - time.monotonic())) is None:
                    return True
            except queue.Empty:
                return False
        return False

    def close(self) -> None:
        self.response.close()
        self._client.close()
        self._thread.join(5)


class DeviceClient:
    """A signed `websockets` client of `GET /v1/device/ws`; every hint is validated against its contract."""

    def __init__(self, connection: ClientConnection) -> None:
        self.connection = connection

    def hint(self, timeout: float = WAIT_S) -> dict[str, Any]:
        text = self.connection.recv(timeout=timeout)
        assert isinstance(text, str)
        hint: dict[str, Any] = json.loads(text)
        contracts_registry().validate("ws/device/hint.json", hint)
        return hint

    def heartbeat(self, capacity: dict[str, int] | None = None, **extra: Any) -> None:
        frame = {"type": "heartbeat", "capacity": capacity or dict(CAPACITY), "sessions": [], "vms": [], **extra}
        contracts_registry().validate("ws/device/heartbeat.json", frame)
        self.connection.send(json.dumps(frame))

    def send_raw(self, data: str | bytes) -> None:
        self.connection.send(data)

    def close_code(self, timeout: float = WAIT_S) -> int | None:
        """Wait for the hub to close the socket; its close code (hints still queued are skipped)."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                self.connection.recv(timeout=max(0.01, deadline - time.monotonic()))
        except Exception:
            pass
        received = self.connection.protocol.close_rcvd
        return None if received is None else received.code

    def close(self) -> None:
        self.connection.close()


@dataclass
class LiveHub:
    """A hub app served by uvicorn on a loopback port."""

    app: FastAPI
    server: uvicorn.Server
    port: int
    clock: FixedClock
    keys: KeyRing
    thread: threading.Thread
    opened: list[Callable[[], None]] = field(default_factory=list)

    @property
    def service(self) -> StreamService:
        found = service_of(self.app)
        assert found is not None
        return found

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stream(self, key: SigningKey, **query: Any) -> SseClient:
        """Open `GET /v1/stream` (query values may be lists) and start reading; refusals raise `Refused`."""
        reader = open_stream(self.base, self.clock, key, **query)
        self.opened.append(reader.close)
        return reader

    def device(self, key: SigningKey, *, headers: dict[str, str] | None = None) -> DeviceClient:
        """Open the device socket (raises on a refused handshake)."""
        opener = ws_connect(
            f"ws://127.0.0.1:{self.port}/v1/device/ws",
            additional_headers=signed_headers(key, self.clock, "/v1/device/ws") if headers is None else headers,
            open_timeout=10,
            max_size=2**20,
        )
        client = DeviceClient(opener.__enter__())
        self.opened.append(client.close)
        return client

    def stop(self) -> None:
        for close in reversed(self.opened):
            with suppress(Exception):
                close()
        self.server.should_exit = True
        self.thread.join(20)


def open_stream(base: str, clock: Callable[[], datetime], key: SigningKey, **query: Any) -> SseClient:
    """Open `GET /v1/stream` at `base` and start reading; a refusal raises `Refused`."""
    params = [(name, item) for name, value in query.items() for item in (value if isinstance(value, list) else [value])]
    target = "/v1/stream" + ("?" + urlencode(params) if params else "")
    client = httpx.Client(timeout=httpx.Timeout(10.0, read=None))
    headers = {**signed_headers(key, clock, target), "Accept": "text/event-stream"}
    response = client.send(client.build_request("GET", base + target, headers=headers), stream=True)
    if response.status_code != 200:
        response.read()
        client.close()
        raise Refused(response.status_code, response.json(), dict(response.headers))
    return SseClient(response, client)


class Refused(Exception):
    """A refused stream request: status, error body and headers."""

    def __init__(self, status: int, body: Any, headers: dict[str, str]) -> None:
        super().__init__(status, body)
        self.status = status
        self.body = body
        self.headers = headers


@contextmanager
def live_hub(harness: HubHarness, port: int, *, config: HubConfig | None = None, **options: Any) -> Iterator[LiveHub]:
    """Serve a second app of the harness's configuration, keys, clock and pool with uvicorn."""
    harness_service = service_of(harness.app)
    if harness_service is not None:
        LIVE.detach(harness_service)
    options.setdefault("run_jobs", False)
    subscribers = dict(signals.subscribers())
    app = create_app(
        config or harness.config, pool=harness.pool, clock=harness.clock, resolvers=harness.keys.resolvers(), **options
    )
    signals.configure(subscribers)  # create_app discovers the subscribers again: keep the test's own
    settings = uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="on", log_level="warning", ws_ping_interval=None
    )
    server = HubServer(settings, app)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline and thread.is_alive():
        time.sleep(0.02)
    assert server.started, "the live hub did not start"
    hub = LiveHub(app, server, port, harness.clock, harness.keys, thread)
    try:
        yield hub
    finally:
        hub.stop()


def wait_for(condition: Callable[[], Any], timeout: float = WAIT_S, interval: float = 0.02) -> Any:
    """Poll until `condition()` is truthy; its value."""
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)
