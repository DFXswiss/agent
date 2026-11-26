"""Device-request handler `vnc.open` (`<area>/requests.py:HANDLERS`; ADR-101, `contracts/requests/vnc.open.*`).

The owner asks for a VNC connection to the screen of a sandbox that waits for a human; the root body names it:

- `{kind: "login", id}` - the check for humans of the `claude.login` request `id` (offered by its `HumanCheck` while
  it waits, `agent_runner.vnc.screens`);
- `{kind: "session", id}` - the platform browser of the session `id`, whose task owner is the requester (the browser
  VM of ADR-085, while it is open).

Only the owner's **own identity** opens a screen: a request of the owner's agent is answered `failed` `forbidden`
(`runner.vnc refused reason=agent`), so an agent never takes over a check that is meant for a person (E-39); the hub
refuses the agent's end of the tunnel as well. No sandbox of the owner with a screen under that target is `no_screen`.

Otherwise the device starts the sandbox's VNC server with a one-time password, draws a fresh tunnel key and answers
`{step: "vnc", key, password, idle_seconds}` (sealed to the requester like every response, never logged). It joins the
tunnel at the hub (`GET /v1/device/vnc/{request}`, signed like the device WebSocket), waits up to `pair_wait` for the
client and then joins the VNC server (`workvm-vnc connect`) with the tunnel (`agent_runner.vnc.tunnel.splice`). The
connection ends when the process the screen was offered for goes on or ends (`ended`: the check passed, the login or
the browser session ended), after `idle` seconds without input (`idle`), when the viewer or the server closes it
(`closed`), when no client joined (`unpaired`) or when the request's time runs out (`timeout`). The VNC server is
stopped on every path; the final response `{step: "done", reason}` names why.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from websockets.exceptions import ConnectionClosed, WebSocketException

from agent_runner.crypto.encoding import b64u_encode
from agent_runner.requests.service import Flow
from agent_runner.sessions import records
from agent_runner.sync.ws import websocket_connect
from agent_runner.vnc.screen import ScreenError, VmScreen, one_time_password
from agent_runner.vnc.screens import Offer, screens_for
from agent_runner.vnc.tunnel import DEVICE, Connect, SocketClosed, Tunnel, TunnelBroken, new_key, splice
from agent_runner.workvm.browser_live import spawn_process
from agent_runner.workvm.service import browsers_for

KINDS = ("login", "session")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TARGET = "/v1/device/vnc/{request}"
PAIRED = '{"type":"paired"}'
IDLE_SECONDS = 120.0
PAIR_SECONDS = 120.0
CHECK_SECONDS = 5.0
MARGIN_SECONDS = 5.0
CLOSED = {"idle": "idle"}
"""Why `splice` ended, as the final response names it (everything else: the viewer or the server closed it)."""


@dataclass(frozen=True)
class VncOptions:
    """Time limits and seams (tests replace them through `context.vnc_options`)."""

    idle: float = IDLE_SECONDS
    pair_wait: float = PAIR_SECONDS
    check: float = CHECK_SECONDS
    connect: Connect = websocket_connect


def options(context: Any) -> VncOptions:
    found = getattr(context, "vnc_options", None)
    return found if isinstance(found, VncOptions) else VncOptions()


def _line(flow: Flow, text: str) -> None:
    flow.context.log.write(f"runner.vnc {text} request_id={flow.id}\n")
    flow.context.log.flush()


async def _fail(flow: Flow, code: str) -> None:
    _line(flow, f"failed code={code}")
    await flow.respond({"step": "failed", "code": code}, final=True)


def target(body: Mapping[str, object]) -> tuple[str, str] | None:
    """`(kind, id)` of a well-formed root body, else None."""
    kind, found = body.get("kind"), body.get("id")
    if set(body) != {"kind", "id"} or kind not in KINDS or not isinstance(found, str) or not UUID_RE.fullmatch(found):
        return None
    return str(kind), found


Watch = Callable[[], Awaitable[None]]


async def session_offer(flow: Flow, session_id: str, opts: VncOptions) -> tuple[Offer, Watch] | None:
    """The screen of the session's open platform browser when the requester owns the session's task; the offer and
    a watch that ends it when the browser session closes or loses its VM."""
    browsers = browsers_for(flow.context)
    if browsers is None:
        return None
    found = await browsers.running(session_id)
    if found is None:
        return None
    _row, vm = found
    async with browsers.db.connection() as conn:
        local = await records.get(conn, session_id)
    if local is None or local.owner_id != flow.user:
        return None
    shell = browsers.workvms.deps.shell_for(vm)

    async def spawn(argv: Any) -> Any:
        return await spawn_process(shell, argv)

    offer = Offer("session", session_id, flow.user, VmScreen(shell, spawn))

    async def watch() -> None:
        while True:
            await asyncio.sleep(opts.check)
            again = await browsers.running(session_id)
            if again is None or again[1].id != vm.id:
                offer.ended.set()
                return

    return offer, watch


class WsSocket:
    """The device's tunnel WebSocket (`websockets`) with the tunnel's closed signal (`SocketClosed`)."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    async def send(self, message: bytes) -> None:
        try:
            await self.raw.send(message)
        except ConnectionClosed:
            raise SocketClosed from None

    async def recv(self) -> Any:
        try:
            return await self.raw.recv()
        except ConnectionClosed:
            raise SocketClosed from None


async def _paired(socket: WsSocket) -> bool:
    try:
        return bool(await socket.recv() == PAIRED)
    except SocketClosed:
        return False


async def _first(*futures: asyncio.Future[Any], timeout: float) -> set[asyncio.Future[Any]]:
    done, _ = await asyncio.wait(futures, timeout=max(timeout, 0.0), return_when=asyncio.FIRST_COMPLETED)
    return done


async def serve(flow: Flow, offer: Offer, key: bytes, opts: VncOptions) -> str:
    """Join the tunnel and the VNC server until the connection ends (module docstring); why it ended."""
    path = TARGET.format(request=flow.id)
    client = flow.service.sync.client
    headers = client.signer.sign("GET", path, b"").headers
    ended = asyncio.ensure_future(offer.ended.wait())
    try:
        async with opts.connect(client.origin.ws_base_url + path, headers) as raw:
            socket = WsSocket(raw)
            paired = asyncio.ensure_future(_paired(socket))
            try:
                done = await _first(paired, ended, timeout=min(opts.pair_wait, flow.remaining() - MARGIN_SECONDS))
            finally:
                paired.cancel()
            if ended in done:
                return "ended"
            if paired not in done:
                return "unpaired"
            if not paired.result():
                return "closed"
            return await _join(flow, offer, Tunnel(key, flow.id, DEVICE), socket, ended=ended, idle=opts.idle)
    except TunnelBroken:
        return "broken"
    except (OSError, TimeoutError, WebSocketException):
        return "unreachable"
    finally:
        ended.cancel()


async def _join(
    flow: Flow, offer: Offer, tunnel: Tunnel, socket: Any, *, ended: asyncio.Future[Any], idle: float
) -> str:
    local = await offer.screen.connect()
    offer.viewers += 1
    _line(flow, "vnc_connected")
    spliced = asyncio.ensure_future(splice(local.reader, local.writer, socket, tunnel, idle=idle))
    try:
        done = await _first(spliced, ended, timeout=flow.remaining() - MARGIN_SECONDS)
        if spliced in done:
            return CLOSED.get(spliced.result(), "closed")
        return "ended" if ended in done else "timeout"
    finally:
        spliced.cancel()
        await asyncio.gather(spliced, return_exceptions=True)
        offer.viewers -= 1
        await local.close()


async def vnc_open(flow: Flow) -> None:
    found = target(flow.body)
    if found is None:
        await _fail(flow, "upstream_error")
        return
    if flow.requester != flow.user:
        _line(flow, "refused reason=agent")  # a screen that waits for a human is taken over by the person only
        await _fail(flow, "forbidden")
        return
    kind, target_id = found
    opts = options(flow.context)
    watch: Watch | None = None
    if kind == "login":
        offer = screens_for(flow.context).find(kind, target_id, flow.user)
    else:
        made = await session_offer(flow, target_id, opts)
        offer, watch = (None, None) if made is None else made
    if offer is None:
        await _fail(flow, "no_screen")
        return
    password, key = one_time_password(), new_key()
    try:
        await offer.screen.start(password)
    except ScreenError as error:
        _line(flow, f"start_failed reason={error.reason}")
        await _fail(flow, "upstream_error")
        return
    watcher = asyncio.ensure_future(watch()) if watch is not None else None
    try:
        _line(flow, f"offered kind={kind}")
        idle = max(1, round(opts.idle))
        body = {"step": "vnc", "key": b64u_encode(key), "password": password, "idle_seconds": idle}
        await flow.respond(body)
        reason = await serve(flow, offer, key, opts)
    finally:
        if watcher is not None:
            watcher.cancel()
        with contextlib.suppress(Exception):
            await offer.screen.stop()
    _line(flow, f"closed reason={reason}")
    await flow.respond({"step": "done", "reason": reason}, final=True)


HANDLERS: Mapping[str, Callable[..., Any]] = {"vnc.open": vnc_open}
