"""The device WebSocket `GET /v1/device/ws` (03-api.md section 6.2, ADR-013).

The upgrade request is signed by the device key (`agent-request-v1` over an empty body) and passes the request
pipeline like every signed request; an unknown or refused key (`key_revoked` for a decertified or removed device)
closes the handshake with 1008 before it is accepted. The device must be `active` (`agent_hub.stream.presence`).

**From the hub**: `hello {hub_time}` right after the upgrade, then the content-free hints published for this device
(`agent_hub.stream.publish`; a hint is `{"type": <hint>, ...identifiers}`). The device always fetches the data by a
signed HTTP request. Hints wait in a per-socket buffer of `HINT_BUFFER`; on overflow the buffer is replaced by one
`reconcile` (the device then pulls its whole state). After `decertified` the socket is closed (1008). Hints are never
queued for a device that is not connected.

**From the device**: `heartbeat {capacity, sessions, vms}` every 15 s (any other frame, a binary frame or a frame
over `MAX_FRAME_BYTES` closes the socket with 1008/1003/1009). Each heartbeat refreshes `device_presence` and
`device.capacity`; the session and VM lists are checked but not stored (session and VM states reach the hub through
the outbox). A socket without a heartbeat for `AGENT_HUB_DEVICE_ONLINE_SECONDS` is closed (1008): the device is
offline. A newer socket of the same device replaces an older one (closed with 1008 in this process; in another
process its next heartbeat finds the presence row taken over and closes it).

Close codes: 1008 policy (refused, superseded, decertified, removed, missing heartbeat, invalid frame), 1003 binary
frame, 1009 frame too large, 1011 internal failure (logged), 1012 hub shutting down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from starlette.websockets import WebSocket, WebSocketDisconnect

LOG = logging.getLogger("agent_hub.stream")
HINT_BUFFER = 256
MAX_FRAME_BYTES = 64 * 1024
MAX_SAFE_INTEGER = 2**53 - 1
POLICY = 1008
UNSUPPORTED = 1003
TOO_LARGE = 1009
INTERNAL = 1011
RESTART = 1012
CAPACITY_KEYS = frozenset(
    {"vm_memory_budget_mib", "vm_memory_used_mib", "vm_slots_total", "vm_slots_free", "pool_ready"}
)
OPTIONAL_CAPACITY_KEYS = frozenset(
    {
        "cpus",
        "memory_total_mib",
        "disk_free_mib",
        "vms_running",
        "sessions_active",
        "work_vms_running",
        "sessions_per_vm",
        "vms_max",
    }
)
"""Counters and settings a runner may add to its capacity (ADR-060 section 2)."""
FEATURES_KEY = "features"
MAX_FEATURES = 16
_FEATURE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
SESSION_STATES = frozenset(
    {
        "requested",
        "vm_assigning",
        "vm_ready",
        "sandbox_ready",
        "agent_starting",
        "prompt_delivering",
        "working",
        "idle",
        "suspended",
        "resuming",
        "stopping",
        "stopped",
        "failed",
    }
)
VM_STATES = frozenset({"pool", "assigning", "running", "stopping", "stopped", "deleted"})
MAX_LIST_ITEMS = 1000
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_VM_ID_RE = re.compile(r"vm-[0-9a-f]{8}")


class FrameInvalid(ValueError):
    """A frame from the device breaks contracts/ws/device/heartbeat.json."""


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_SAFE_INTEGER:
        raise FrameInvalid("count")
    return value


def _items(value: object, keys: tuple[str, str], pattern: re.Pattern[str], states: frozenset[str]) -> None:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise FrameInvalid("list")
    for item in value:
        if not isinstance(item, dict) or set(item) != set(keys):
            raise FrameInvalid("list item")
        ident, state = item[keys[0]], item[keys[1]]
        if not isinstance(ident, str) or pattern.fullmatch(ident) is None or state not in states:
            raise FrameInvalid("list item")


def _features(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_FEATURES:
        raise FrameInvalid("features")
    for item in value:
        if not isinstance(item, str) or _FEATURE_RE.fullmatch(item) is None:
            raise FrameInvalid("features")
    return list(value)


def parse_heartbeat(text: str) -> dict[str, Any]:
    """The capacity of a heartbeat frame (`FrameInvalid` for anything else): the five counters, optionally the further
    counters and settings and the features of ADR-060 section 2."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise FrameInvalid("json") from error
    if not isinstance(data, dict) or data.get("type") != "heartbeat":
        raise FrameInvalid("type")
    if set(data) != {"type", "capacity", "sessions", "vms"}:
        raise FrameInvalid("fields")
    capacity = data["capacity"]
    if not isinstance(capacity, dict) or not CAPACITY_KEYS <= set(
        capacity
    ) <= CAPACITY_KEYS | OPTIONAL_CAPACITY_KEYS | {FEATURES_KEY}:
        raise FrameInvalid("capacity")
    counts: dict[str, Any] = {key: _count(value) for key, value in capacity.items() if key != FEATURES_KEY}
    if FEATURES_KEY in capacity:
        counts[FEATURES_KEY] = _features(capacity[FEATURES_KEY])
    _items(data["sessions"], ("session_id", "state"), _UUID_RE, SESSION_STATES)
    _items(data["vms"], ("vm_id", "state"), _VM_ID_RE, VM_STATES)
    return counts


def hint_text(hint_type: str, body: Mapping[str, Any]) -> str:
    """The JSON frame of a hint."""
    return json.dumps({"type": hint_type, **body}, separators=(",", ":"))


@dataclass(eq=False)
class DeviceSocket:
    """One accepted device socket: its identity, its connection time (the presence row it owns) and its hints."""

    device_id: UUID
    websocket: WebSocket
    connected_at: datetime
    buffer: int = HINT_BUFFER
    close_code: int | None = None
    _hints: deque[str] = field(default_factory=deque)
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, hint_type: str, body: Mapping[str, Any]) -> None:
        """Queue a hint (event-loop thread); an overflow leaves one `reconcile`; `decertified` ends the socket."""
        if self.close_code is not None:
            return
        if len(self._hints) >= self.buffer:
            self._hints.clear()
            self._hints.append(hint_text("reconcile", {}))
        self._hints.append(hint_text(hint_type, body))
        if hint_type == "decertified":
            self.close_code = POLICY
        self._wake.set()

    def close(self, code: int) -> None:
        """End the socket with `code` after the queued hints."""
        if self.close_code is None:
            self.close_code = code
        self._wake.set()

    async def send_loop(self) -> None:
        """Send the hints until the socket is closed by the hub."""
        while True:
            while self._hints:
                await self.websocket.send_text(self._hints.popleft())
            if self.close_code is not None:
                return
            self._wake.clear()
            await self._wake.wait()

    async def receive_loop(self, window: float, on_heartbeat: Any) -> None:
        """Read heartbeats until the device leaves, stays silent for `window` seconds or sends an invalid frame."""
        while self.close_code is None:
            try:
                message = await asyncio.wait_for(self.websocket.receive(), window)
            except TimeoutError:
                self.close(POLICY)
                return
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(int(message.get("code", 1000)))
            text = message.get("text")
            if text is None:
                self.close(UNSUPPORTED)
                return
            if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                self.close(TOO_LARGE)
                return
            try:
                capacity = parse_heartbeat(text)
            except FrameInvalid:
                self.close(POLICY)
                return
            if not await on_heartbeat(capacity):
                self.close(POLICY)
                return
