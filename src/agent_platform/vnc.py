"""Taking over the screen of a sandbox by VNC when a process needs a human (ADR-101; rule E-39 of the client: "ich
will keinen link, ich will dass du alles erledigst und wenn ich eingreifen muss dann gibst du mir kurz vnc").

`agent-platform vnc (--login REQUEST | --session ID | --task ID) [--wait S]`:

1. Only the user's own identity opens a screen (`invalid_argument` `human_only` for an agent without it): a check for
   humans is answered by the person, never by a model. The device and the hub check it again.
2. The target: `--login` the `claude.login` request whose check for humans waits (its device), `--session` the
   session whose platform browser is open (the session's device), `--task` the newest open platform browser of the
   task's sessions.
3. The tool sends `vnc.open {kind, id}` (sealed to the device, slot = the target) and waits up to `--wait` seconds for
   `{step: "vnc", key, password}`; a failure is `device_request_failed` with its code (`no_screen`, `forbidden`, ...).
4. It listens on `127.0.0.1` only (a free port) and opens the viewer itself, no link to click: on macOS the built-in
   Screen Sharing (`open vnc://:<password>@127.0.0.1:<port>`), elsewhere `vncviewer` when installed; the port (and,
   without a viewer that takes it, the one-time password) is printed on stderr for the person, never in the result.
5. The first local connection is joined with the hub's tunnel (`GET /v1/vnc/{request}`, signed; the records are
   encrypted with the key of step 3, `agent_runner.vnc.tunnel`); no further local connection is accepted. The command
   returns when the connection ends: the check passed (`ended`), idle, closed by the viewer, or the request's time ran
   out; the result names the device's reason.

`take_over` is the same for a login the tool follows itself (`subscriptions add|login --vnc`): when the device answers
`needs_human` with `vnc_available`, the tool opens the connection on its own and then follows the login again.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

from agent_platform import browser
from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.devicereq import Keys, device_keys, finish, resume, submit, wait
from agent_platform.errors import PlatformError
from agent_platform.eventio import uuid_argument
from agent_platform.hub import segment
from agent_platform.sessions import session_row
from agent_platform.wsclient import connect as websocket_connect
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.vnc.tunnel import CLIENT, Connect, Tunnel, close_writer, splice

KIND = "vnc.open"
FEATURE = "vnc"
STEPS = frozenset({"vnc", "done", "failed"})
HOST = "127.0.0.1"
ACCEPT_SECONDS = 120.0
PAIR_SECONDS = 120.0
DEFAULT_WAIT_S = 60
PAIRED = '{"type":"paired"}'
Opener = Callable[[int, str], str | None]


def human_only(account: Account) -> None:
    """Only the user's own identity takes over a screen (module docstring point 1)."""
    if account.identity is None:
        raise PlatformError("invalid_argument", reason="human_only")


def open_viewer(port: int, password: str, *, platform: str = sys.platform) -> str | None:
    """Start the person's VNC viewer on the local end; which one (None: none found, the person starts one)."""
    if platform == "darwin":
        subprocess.Popen(["open", f"vnc://:{password}@{HOST}:{port}"])
        return "screen_sharing"
    viewer = shutil.which("vncviewer")
    if viewer is not None:
        subprocess.Popen([viewer, f"{HOST}::{port}"])
        return "vncviewer"
    return None


def _tell(port: int, password: str, viewer: str | None) -> None:
    if viewer == "screen_sharing":
        print(f"agent-platform: Screen Sharing opens the screen on {HOST}:{port}", file=sys.stderr)
        return
    print(f"agent-platform: VNC on {HOST}:{port}, one-time password {password}", file=sys.stderr)


async def bridge(
    url: str,
    headers: Mapping[str, str],
    key: bytes,
    request_id: str,
    password: str,
    *,
    opener: Opener,
    connect: Connect = websocket_connect,
    accept_seconds: float = ACCEPT_SECONDS,
    pair_seconds: float = PAIR_SECONDS,
) -> str:
    """Listen on the loopback, open the viewer and join its connection with the tunnel; why it ended locally:
    `not_opened` (no viewer connected in time), `unpaired` (the device did not join), or the end of `splice`."""
    accepted: asyncio.Queue[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = asyncio.Queue()
    taken: list[bool] = []

    async def arrive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if accepted.empty() and not taken:
            accepted.put_nowait((reader, writer))
            return
        await close_writer(writer)  # one viewer per connection

    server = await asyncio.start_server(arrive, HOST, 0)
    port = int(server.sockets[0].getsockname()[1])
    _tell(port, password, opener(port, password))
    try:
        reader, writer = await asyncio.wait_for(accepted.get(), accept_seconds)
    except TimeoutError:
        return "not_opened"
    finally:
        server.close()
    taken.append(True)
    try:
        async with connect(url, dict(headers)) as socket:
            try:
                paired = await asyncio.wait_for(socket.recv(), pair_seconds)
            except TimeoutError:
                return "unpaired"
            if paired != PAIRED:
                return "unpaired"
            return await splice(reader, writer, socket, Tunnel(key, request_id, CLIENT))
    finally:
        await close_writer(writer)


def request(account: Account, device: str, keys: Keys, kind: str, target: str) -> dict[str, Any]:
    """Send `vnc.open {kind, id}` to the device (slot = the target)."""
    return submit(account, (device, keys), KIND, {"kind": kind, "id": target}, slot=target)


def connect_offered(
    account: Account, prepared: dict[str, Any], offered: dict[str, Any], *, opener: Opener, connect: Connect
) -> str:
    """Bridge the offered connection (step `vnc`) and return the local reason."""
    url, headers = account.upgrade(f"/v1/vnc/{segment(prepared['id'])}")
    key = b64u_decode(offered["key"], 32)
    return asyncio.run(bridge(url, headers, key, prepared["id"], offered["password"], opener=opener, connect=connect))


def take_over(
    account: Account,
    kind: str,
    target: str,
    device: str,
    keys: Keys,
    *,
    seconds: int = DEFAULT_WAIT_S,
    opener: Opener | None = None,
    connect: Connect | None = None,
) -> dict[str, Any]:
    """Ask the device for the screen, bridge it until it ends and read the device's reason (module docstring)."""
    human_only(account)
    prepared = request(account, device, keys, kind, target)
    found = wait(account, prepared, seconds, STEPS)
    if found["step"] != "vnc":
        return finish(prepared, found)
    joined = connect or websocket_connect
    local = connect_offered(account, prepared, found, opener=opener or open_viewer, connect=joined)
    ended = finish(prepared, wait(account, prepared, seconds, frozenset({"done", "failed"})))
    return {**ended, "kind": kind, "id": target, "local": local}


def _session_device(account: Account, session: str) -> str:
    device = session_row(account, session)["session"].get("device_id")
    if not isinstance(device, str):
        raise PlatformError("no_device", reason="session_not_running")
    return device


def _open_browser_session(account: Account, task: str) -> str:
    for row in browser.browsers(account, None, task)["browsers"]:
        if row.get("state") in ("open", "restarted", "starting"):
            return str(row["session_id"])
    raise PlatformError("device_request_failed", failure="no_screen")


def target_of(account: Account, args: Any) -> tuple[str, str, str]:
    """`(kind, id, device)` of the options (module docstring point 2)."""
    given = [name for name in ("login", "session", "task") if getattr(args, name) is not None]
    if len(given) != 1:
        raise PlatformError("invalid_argument", reason="vnc_target")
    if args.login is not None:
        prepared = resume(account, args.login, ("claude.login",))
        return "login", str(prepared["id"]), str(prepared["device"])
    session = uuid_argument(args.session) if args.session is not None else _open_browser_session(account, args.task)
    return "session", session, _session_device(account, session)


def command(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    """`agent-platform vnc` (module docstring)."""
    human_only(account)
    if not 1 <= args.wait <= 600:
        raise PlatformError("invalid_argument")
    kind, target, device = target_of(account, args)
    keys = device_keys(account, device)
    if keys is None:
        raise PlatformError("no_device", reason="device_not_assigned")
    return take_over(account, kind, target, device, keys, seconds=args.wait)


def register(tree: Tree) -> None:
    group = tree.commands
    sub = tree.command(
        group,
        "vnc",
        command,
        "take over the screen of a sandbox that waits for a person (a check for humans) by VNC; only the user",
        example="agent-platform vnc --login <request_id>",
        words="vnc bildschirm fernsteuerung bildschirmfreigabe eingreifen captcha prüfung mensch übernehmen screen "
        "sharing remote control take over human check",
        then="agent-platform subscriptions check --request <request_id>",
    )
    sub.add_argument("--login", help="the claude.login request whose check for humans waits")
    sub.add_argument("--session", help="the session whose platform browser is open")
    sub.add_argument("--task", help="the newest open platform browser of this task's sessions")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to wait for the device (1-600)")
