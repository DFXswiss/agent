"""Session mode: the agent tool as the main orchestrator inside a platform session (ADR-082 sections 6.5 and 9.7,
05-runner.md sections 17.2 and 17.4a).

Inside the VM the tool has no network. It runs under the guest's account `agent-orch` with `AGENT_PLATFORM_RELAY`, the
absolute path of the guest control process's socket `orch.sock`; that variable is session mode. Every signed request
of the tool goes to the hub through that socket: the guest journals it, the runner forwards the bytes unchanged to its
hub and the hub's answer comes back the same way. The tool still signs every request with its own key and verifies
every answer with the pinned hub key (`agent_runner.sync.http.HubClient`); only the transport changes
(`RelayTransport`). Nothing on the way can forge an answer.

- `session_mode(environ)`: the context of one command run (`cli.execute`); `relay_path()` names the socket while a
  command of session mode runs, None otherwise. A relative path is `invalid_argument`.
- `RelayTransport`: an `httpx.AsyncBaseTransport` that sends one JSON line `{"id", "op": "relay", "args": {method,
  target, headers, body}}` (`body` b64u, `headers` only the `Agent-*` request headers and `Content-Type`) and builds
  the response from `{"ok": true, "result": {status, headers, body}}`. A refusal of the guest or the runner
  (`{"ok": false, "error": <code>}`), a broken socket or an answer of the wrong shape is an `httpx.TransportError`,
  which the signed client reports as `hub_unreachable`.
- `refused_in_session(argv)`: every command but the guest's own (`guest_command`) and the allow-list
  `SESSION_ALLOWED` (hub work that prints), and every command with an option that writes a path (`PATH_OPTIONS`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any

import httpx

from agent_platform.errors import PlatformError
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError

RELAY_ENV = "AGENT_PLATFORM_RELAY"
REQUEST_HEADERS = ("Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature", "Content-Type")
RESPONSE_HEADERS = frozenset({"agent-hub-key", "agent-hub-time", "agent-hub-signature", "content-type", "retry-after"})
MAX_LINE = 16 * 1024 * 1024
"""Upper bound of one answer line: an answer body of 8 MiB (the runner's limit) as b64u with its frame."""
TIMEOUT_S = 60.0
"""The guest waits 40 s for the runner's answer; the tool waits a little longer for the guest's answer."""
# Commands allowed in session mode (an allow-list, review KOR-57 b no. 4): what the main orchestrator does for its user
# through signed hub requests and prints. Not here: a local login or browser, pairing, registration or recovery,
# installing anything on a computer, secrets printed into the session (passkeys, recovery codes, tokens), long-running
# servers, and every command whose option writes a path (`PATH_OPTIONS`).
SESSION_ALLOWED: tuple[tuple[str, ...], ...] = (
    ("guide",),
    ("search",),
    ("status",),
    ("api",),
    ("diagnose",),
    ("settings", "set"),
    ("credentials", "list"),
    ("credentials", "check"),
    ("credentials", "rotate"),
    ("org",),
    ("wg",),
    ("device", "assign"),
    ("device", "list"),
    ("device", "unassign"),
    ("device", "rename"),
    ("device", "invite"),
    ("task",),
    ("member",),
    ("whiteboard",),
    ("session",),
    ("repos",),
    ("notifications",),
    ("quotas",),
    ("monitoring",),
    ("audit",),
    ("agents", "list"),
    ("agents", "revoke"),
    ("security",),
    ("github", "status"),
    ("github", "accounts"),
    ("test", "list"),
    ("test", "show"),
    ("test", "log"),
    ("billing",),
    ("community", "status"),
    ("community", "messages"),
    ("community", "post"),
    ("community", "delete"),
    ("community", "mutes"),
    ("community", "mute"),
    ("community", "unmute"),
)
PATH_OPTIONS = ("--output", "--config")
"""Options that make the tool write a path of its own account (its key store among them): refused in session mode."""
BARE = frozenset({"--version", "--help", "-h"})

_PATH: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_platform_relay", default=None)


def relay_path() -> str | None:
    """The relay socket of the command that runs now (session mode), None outside session mode."""
    return _PATH.get()


@contextlib.contextmanager
def session_mode(environ: Mapping[str, str]) -> Iterator[str | None]:
    """Run a command in the mode `environ` names (module docstring); yields the relay path or None."""
    path = environ.get(RELAY_ENV, "") or None
    if path is not None and not path.startswith("/"):
        raise PlatformError("invalid_argument")
    token = _PATH.set(path)
    try:
        yield path
    finally:
        _PATH.reset(token)


def command_words(argv: Sequence[str]) -> list[str]:
    return [word for word in argv if not word.startswith("-")]


def guest_command(argv: Sequence[str]) -> bool:
    """The commands only the guest control process runs (05-runner.md section 17.4a): `pair --session [--handover]`
    and `mcp --socket <path>`."""
    if argv[:1] == ["pair"]:
        return argv[1:2] == ["--session"] and argv[2:] in ([], ["--handover"])
    socket_form = (len(argv) == 3 and argv[1] == "--socket") or (len(argv) == 2 and argv[1].startswith("--socket="))
    return argv[:1] == ["mcp"] and socket_form


def refused_in_session(argv: Sequence[str]) -> bool:
    """Everything but the guest's commands and `SESSION_ALLOWED` without a path-writing option (module docstring)."""
    argv = list(argv)
    if any(arg == option or arg.startswith(option + "=") for arg in argv for option in PATH_OPTIONS):
        return True
    if guest_command(argv):
        return False
    words = command_words(argv)
    if not words:
        return not set(argv) <= BARE
    return not any(words[: len(allowed)] == list(allowed) for allowed in SESSION_ALLOWED)


class _Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.data


def _answer(line: bytes) -> tuple[int, dict[str, str], bytes]:
    """`(status, headers, body)` of the guest's answer line; `httpx.TransportError` for a refusal or a wrong shape."""
    try:
        message = json.loads(line)
    except (ValueError, RecursionError):
        raise httpx.RemoteProtocolError("relay answer invalid") from None
    if not isinstance(message, dict) or message.get("id") != 1:
        raise httpx.RemoteProtocolError("relay answer invalid")
    if message.get("ok") is not True:
        raise httpx.ConnectError("relay refused")
    result = message.get("result")
    if not isinstance(result, dict) or set(result) != {"status", "headers", "body"}:
        raise httpx.RemoteProtocolError("relay answer invalid")
    status, headers, body = result["status"], result["headers"], result["body"]
    if type(status) is not int or not 100 <= status <= 599 or not isinstance(headers, dict):
        raise httpx.RemoteProtocolError("relay answer invalid")
    if not all(isinstance(k, str) and isinstance(v, str) and k.lower() in RESPONSE_HEADERS for k, v in headers.items()):
        raise httpx.RemoteProtocolError("relay answer invalid")
    try:
        raw = b64u_decode(body) if body != "" else b""
    except (CryptoError, ValueError, TypeError):
        raise httpx.RemoteProtocolError("relay answer invalid") from None
    return status, headers, raw


class RelayTransport(httpx.AsyncBaseTransport):
    """Requests through the guest's relay socket (module docstring)."""

    def __init__(self, path: str, timeout: float = TIMEOUT_S) -> None:
        self.path = path
        self.timeout = timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        headers = {name: request.headers[name] for name in REQUEST_HEADERS if name in request.headers}
        args: dict[str, Any] = {
            "method": request.method,
            "target": request.url.raw_path.decode("ascii"),
            "headers": headers,
            "body": b64u_encode(body) if body else "",
        }
        frame = json.dumps({"id": 1, "op": "relay", "args": args}, separators=(",", ":")) + "\n"
        try:
            line = await asyncio.wait_for(self._exchange(frame.encode("ascii")), self.timeout)
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            raise httpx.ConnectError("relay unreachable") from None
        status, answer_headers, raw = _answer(line)
        return httpx.Response(status, headers=answer_headers, stream=_Body(raw), request=request)

    async def _exchange(self, frame: bytes) -> bytes:
        reader, writer = await asyncio.open_unix_connection(self.path, limit=MAX_LINE)
        try:
            writer.write(frame)
            await writer.drain()
            return await reader.readuntil(b"\n")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


def transport() -> RelayTransport | None:
    """The transport of a signed client: the relay in session mode, else None (the network)."""
    path = relay_path()
    return None if path is None else RelayTransport(path)
