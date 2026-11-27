"""Device WebSocket client (03-api.md section 6.2, 05-runner.md section 2.3, ADR-013).

`GET /v1/device/ws` with a signed upgrade request (`agent-request-v1` over an empty body). The socket carries hints
only; the upgrade response and the frames are not response-signed, so nothing read from it is ever acted upon except
through a signed HTTP fetch by the hint handlers.

- After every (re)connect: the `reconcile` connect hooks, then an outbox push (`Pusher.drain`), then the `pull`
  connect hooks (`sync.extensions`), then hints are dispatched in arrival order (`sync.hints`).
- Heartbeat every 15 s: `{"type":"heartbeat","capacity":...,"sessions":[...],"vms":[...]}` from the registered
  providers, with every open session and every VM (ADR-088 section 3): when they do not fit into one frame of
  `MAX_FRAME_BYTES` (or a list would have more than `MAX_LIST_ITEMS`), several complete heartbeat frames follow each
  other, each with the same capacity and its share of `sessions` and `vms` (`split_heartbeat`; the hub uses every
  frame on its own). `beat_soon()` sends the next one at once (the VM area calls it on every VM state change, so the
  hub's start estimate, which depends on `pool_ready`, is not up to 15 s old). An early frame waits `EARLY_BEAT_S` (the
  outbox push spacing) first, so a burst of VM changes (pool refill) coalesces into one frame.
- A normal close by the hub (also when a heartbeat send meets it first) is `ws_closed`, everything else `ws_failed`.
- The upgrade request announces the runner's capabilities in the header `Agent-Features` (`features`; ADR-060: the
  hub admits accounts into an org only while every device of it announced `log.admit`; ADR-075: `log.org_assign`
  for assignments to the whole organization, `vm.choice` for the VM of each session named in its transitions;
  KOR-65: the GitHub ways the web may offer for this device; ADR-078: `github.identities`, the hub stores GitHub
  identity choices and defaults only while every active device of the organization announced it; ADR-079:
  `log.policy` for the switches, the operator-subscription list and the role `operator` of the platform log; ADR-096:
  `log.support` for the support release and the role `support`). A
  header, not a query parameter: an older hub refuses unknown query parameters but ignores unknown headers.
- Reconnect backoff 1 s doubling to 30 s with ±20 % jitter, reset after a connection that stayed up 30 s
  (legacy `cmd_sync` rule). Redirects are never followed; no proxy is used.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, TextIO

from websockets.asyncio.client import connect as _ws_connect
from websockets.exceptions import ConnectionClosedOK, WebSocketException

from agent_runner.sync import outbox
from agent_runner.sync.backoff import Backoff
from agent_runner.sync.extensions import ConnectHook, HeartbeatSection
from agent_runner.sync.hints import dispatch
from agent_runner.sync.http import HubRefusal, HubTransportError, Signer, format_time
from agent_runner.sync.push import Pusher, SyncFatal

WS_TARGET = "/v1/device/ws"
FEATURES_HEADER = "Agent-Features"
FEATURES = (
    "log.admit",
    "log.org_assign",
    "log.policy",
    "log.support",
    "vm.choice",
    "github.ways",
    "github.identities",
    "prompt.withdraw",
    "subscriptions",
    "agent.profiles",
    "platform.analysis",
    "subscriptions.parallel",
)
"""What this runner understands: `log.admit` - membership logs with `admit` entries and platform logs with
`user_certify`/`user_decertify` (the copied verifier of `agent_runner.crypto.log`); `github.ways` - it announces the
ways of "Connect GitHub" it can run (`features`, KOR-65), so a device without them offers only an existing token;
`prompt.withdraw` - the withdrawal of waiting prompts through `prompt_queue.withdrawn` of the device state (ADR-083
section 3); `subscriptions` - `claude.login` for further subscriptions (`subscription_id`, `name`, `note`, `email`;
ADR-094 section 2), so clients send those fields only to such a device;
`agent.profiles` - the rights profiles of the section `agent_profiles` (ADR-094 section 3); `platform.analysis` - the
session read tools `get_usage`, `list_audit`, `list_workgroup_operations` and `get_metrics` (ADR-097 section 3), so the
hub creates an analysis task only while every device of the organization announced it; `subscriptions.parallel` -
logins of different subscriptions side by side (slot), the record's own mailbox, `account`, `extended`, `click`,
`relay` and `mailbox.check {subscription_id}` (ADR-100 section 7), so clients send those only to such a device."""
DEVICE_FLOW = "github.device_flow"
OAUTH_APP = "github.oauth_app"
OPERATOR_GITHUB = "github.operator"


def features(context: Any) -> tuple[str, ...]:
    """`FEATURES` and the GitHub ways set up on this device (KOR-65): `github.device_flow` - a device flow (the OAuth
    App or the GitHub App) has its client ID; `github.oauth_app` - the OAuth App's (its token reaches every
    repository of the account; the web sends the step "connect" of a held operation only to such a device);
    `github.operator` - the operator's GitHub account is configured (users approved for it connect without a device
    flow, ADR-053)."""
    config = getattr(context, "config", None)
    found = list(FEATURES)
    oauth = getattr(config, "github_oauth_client_id", None)
    if oauth or getattr(config, "github_client_id", None):
        found.append(DEVICE_FLOW)
    if oauth:
        found.append(OAUTH_APP)
    if getattr(config, "operator_github_token_file", None) is not None:
        found.append(OPERATOR_GITHUB)
    return tuple(found)


HEARTBEAT_S = 15.0
EARLY_BEAT_S = 0.25
STABLE_S = 30.0
OPEN_TIMEOUT_S = 10.0
MAX_FRAME_BYTES = 64 * 1024
EMPTY_CAPACITY = {
    "vm_memory_budget_mib": 0,
    "vm_memory_used_mib": 0,
    "vm_slots_total": 0,
    "vm_slots_free": 0,
    "pool_ready": 0,
}


MAX_LIST_ITEMS = 1000
"""Items per list of one heartbeat frame (`contracts/ws/device/heartbeat.json`, the hub's `stream.ws`)."""
SPLIT_LISTS = ("sessions", "vms")


def split_heartbeat(frame: Mapping[str, object]) -> list[str]:
    """The heartbeat as one or more complete frames (module docstring): each carries the same capacity and its share of
    `sessions` and `vms`, in order, at most `MAX_FRAME_BYTES` bytes and `MAX_LIST_ITEMS` items per list."""
    base = {**frame, **{name: [] for name in SPLIT_LISTS}}
    base_size = len(_dumps(base))
    frames: list[dict[str, list[object]]] = []
    current: dict[str, list[object]] = {name: [] for name in SPLIT_LISTS}
    size = base_size
    for name in SPLIT_LISTS:
        items = frame.get(name)
        for item in items if isinstance(items, list) else []:
            added = len(_dumps(item)) + (1 if current[name] else 0)
            if (size + added > MAX_FRAME_BYTES or len(current[name]) >= MAX_LIST_ITEMS) and size > base_size:
                frames.append(current)
                current, size = {key: [] for key in SPLIT_LISTS}, base_size
                added = len(_dumps(item))
            current[name].append(item)
            size += added
    frames.append(current)
    return [_dumps({**base, **share}) for share in frames]


def _dumps(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


class Socket(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def send(self, message: str) -> None: ...


Connector = Callable[[str, Mapping[str, str]], AbstractAsyncContextManager[Socket]]


class _NoRedirect(_ws_connect):
    """`websockets` connect that treats every redirect as a failed handshake."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


def websocket_connect(url: str, headers: Mapping[str, str]) -> AbstractAsyncContextManager[Socket]:
    """Open the hub socket: no redirects, no proxy, no compression, frames of at most 64 KiB."""
    return _NoRedirect(
        url,
        additional_headers=dict(headers),
        open_timeout=OPEN_TIMEOUT_S,
        max_size=MAX_FRAME_BYTES,
        compression=None,
        proxy=None,
    )


class WsClient:
    def __init__(
        self,
        *,
        context: Any,
        signer: Signer,
        ws_base_url: str,
        handlers: Mapping[str, Callable[..., Any]],
        hooks: Mapping[str, list[ConnectHook]],
        sections: Mapping[str, HeartbeatSection],
        pusher: Pusher,
        log: TextIO,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        backoff: Backoff | None = None,
        connect: Connector = websocket_connect,
    ) -> None:
        self.context = context
        self.signer = signer
        self.url = ws_base_url + WS_TARGET
        self.handlers = dict(handlers)
        self.hooks = {phase: list(items) for phase, items in hooks.items()}
        self.sections = dict(sections)
        self.pusher = pusher
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.backoff = backoff or Backoff()
        self.connect = connect
        self.connects = 0
        # True while a socket is open (metric `agent_runner_device_hub_connected`, RUN-15).
        self.connected = False
        self._beat = asyncio.Event()

    def beat_soon(self) -> None:
        """Send the next heartbeat now instead of at the end of the period (module docstring)."""
        self._beat.set()

    def line(self, text: str) -> None:
        self.log.write(f"runner.sync {text}\n")
        self.log.flush()

    async def heartbeat_frame(self) -> dict[str, object]:
        frame: dict[str, object] = {"type": "heartbeat", "capacity": dict(EMPTY_CAPACITY), "sessions": [], "vms": []}
        for name, section in sorted(self.sections.items()):
            try:
                frame[name] = await section.provide(self.context)
            except Exception as exc:
                self.line(f"heartbeat_section_failed field={name} error={type(exc).__name__}")
        return frame

    async def _heartbeats(self, socket: Socket) -> None:
        while True:
            self._beat.clear()
            for frame in split_heartbeat(await self.heartbeat_frame()):
                await socket.send(frame)
            await self._next_beat()

    async def _next_beat(self) -> None:
        period = asyncio.ensure_future(self.sleep(HEARTBEAT_S))
        early = asyncio.ensure_future(self._beat.wait())
        try:
            await asyncio.wait((period, early), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (period, early):
                waiter.cancel()
            await asyncio.gather(period, early, return_exceptions=True)
        if not early.cancelled():
            await self.sleep(EARLY_BEAT_S)

    async def _hooks(self, phase: str) -> None:
        for hook in self.hooks.get(phase, []):
            try:
                await hook.run(self.context)
            except Exception as exc:
                self.line(f"connect_hook_failed name={hook.name} error={type(exc).__name__}")

    async def on_connect(self) -> None:
        """Record the connect time, then reconcile, push the outbox, pull (03-api.md section 6.2)."""
        async with self.context.pool.connection() as conn:
            await outbox.set_state(conn, outbox.WS_LAST_CONNECT, format_time(self.signer.wall()))
        await self._hooks("reconcile")
        try:
            await self.pusher.drain()
        except SyncFatal as fatal:
            self.line(f"connect_push_stopped code={fatal.code}")
        except HubRefusal as refusal:
            self.line(f"connect_push_refused error={refusal.code}")
        except HubTransportError as error:
            self.line(f"connect_push_failed reason={error.reason}")
        await self._hooks("pull")

    async def _receive(self, socket: Socket) -> None:
        await self.on_connect()
        async for message in socket:
            await dispatch(self.handlers, self.context, message, self.log)

    async def _session(self, socket: Socket, stopping: asyncio.Event) -> None:
        self.connected = True
        tasks = [
            asyncio.ensure_future(self._heartbeats(socket)),
            asyncio.ensure_future(self._receive(socket)),
            asyncio.ensure_future(stopping.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.connected = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            task.result()

    async def run(self, stopping: asyncio.Event) -> None:
        """Keep the socket connected until `stopping`."""
        while not stopping.is_set():
            started: float | None = None
            signed = self.signer.sign("GET", WS_TARGET, b"")
            try:
                headers = {**signed.headers, FEATURES_HEADER: ",".join(features(self.context))}
                async with self.connect(self.url, headers) as socket:
                    started = self.clock()
                    self.connects += 1
                    self.line(f"ws_connected count={self.connects}")
                    await self._session(socket, stopping)
                self.line("ws_closed")
            except ConnectionClosedOK:
                # the hub closed normally; a heartbeat sent at that moment sees it first (not a failure)
                self.line("ws_closed")
            except (OSError, TimeoutError, WebSocketException) as exc:
                self.line(f"ws_failed error={type(exc).__name__}")
            if started is not None and self.clock() - started >= STABLE_S:
                self.backoff.reset()
            if stopping.is_set():
                return
            await self.sleep(self.backoff.next())
