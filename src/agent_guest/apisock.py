"""``api.sock``: JSON-lines request/response between the API process and the control process (05-runner.md §5.1).

The API process (user ``platform``) never opens the journal; it sends records and pending calls to the control
process over ``/run/agent-guest/api.sock`` (root:``platform`` 0660). One line per message, UTF-8 JSON objects:

* request ``{"id": <int>, "op": "<name>", "args": {...}}``
* response ``{"id": <same>, "ok": true, "result": {...}}`` or ``{"id": <same>, "ok": false, "error": "<code>",
  "detail"?: ["<pointer>", ...]}``

A connection carries any number of sequential requests. Lines longer than ``MAX_LINE`` bytes end the connection
after an error answer (``size_exceeded``). Ops are the core ops of the control process plus the discovered
``control_routes/<name>.py:API_OPS``; an op raises an exception with a ``code`` attribute (and optional
``detail``) to answer an error; anything else is logged and answered ``unavailable``.

Interface:

* ``ApiSockServer(path, ops, context)`` - threading unix-socket server (bound by the caller, see ``control.py``)
* ``ApiClient(path, timeout=15.0)`` - thread-safe client with a small connection pool:
  ``call(op, args, timeout=None) -> dict`` raises ``ApiSockError(code, detail)``; helpers ``hello()``,
  ``resolve_session(token, peer_uid=None) -> dict | None`` (raises ``AuthLimited`` while the failure limit of the
  connecting UID holds, ``authguard.py``), ``append(kind, body, session_id=None) -> int``,
  ``pending_create(session_id, kind, body) -> str``, ``pending_wait(pending_id, session_id, timeout) -> dict | None``
"""

from __future__ import annotations

import hashlib
import json
import socket
import socketserver
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from typing import Any

from agent_guest.routing import parse_json

MAX_LINE = 4 * 1024 * 1024
POOL_SIZE = 8
WAIT_MARGIN = 5.0

ApiOp = Callable[[Any, dict[str, Any]], dict[str, Any]]


def token_sha256(token: str) -> str:
    """Hex SHA-256 of a session bearer token (the only form stored and sent over ``api.sock``)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ApiSockError(Exception):
    def __init__(self, code: str, detail: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail or []


class AuthLimited(Exception):
    """The connecting UID has failed too many token checks; retry after ``retry_after`` seconds (ATK-36)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


def encode(message: Mapping[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


class ApiSockServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, path: str, ops: Mapping[str, ApiOp], context: Any) -> None:
        self.ops = dict(ops)
        self.context = context
        super().__init__(path, ApiSockHandler)


class ApiSockHandler(socketserver.StreamRequestHandler):
    server: ApiSockServer

    def handle(self) -> None:
        while True:
            line = self.rfile.readline(MAX_LINE + 1)
            if not line:
                return
            if len(line) > MAX_LINE or not line.endswith(b"\n"):
                self.wfile.write(encode({"id": None, "ok": False, "error": "size_exceeded"}))
                return
            self.wfile.write(encode(self._answer(line)))

    def _answer(self, line: bytes) -> dict[str, Any]:
        try:
            message = parse_json(line)
        except ValueError:
            return {"id": None, "ok": False, "error": "request_invalid"}
        ident = message.get("id") if isinstance(message, dict) else None
        if type(ident) is not int:
            return {"id": None, "ok": False, "error": "request_invalid"}
        op_name, args = message.get("op"), message.get("args", {})
        if set(message) - {"id", "op", "args"} or not isinstance(op_name, str) or not isinstance(args, dict):
            return {"id": ident, "ok": False, "error": "request_invalid"}
        op = self.server.ops.get(op_name)
        if op is None:
            return {"id": ident, "ok": False, "error": "not_found"}
        try:
            result = op(self.server.context, args)
        except Exception as error:
            code = getattr(error, "code", None)
            if not isinstance(code, str):
                sys.stderr.write(f"agent-guest-control: api op {op_name} failed\n")
                traceback.print_exc(file=sys.stderr)
                code = "unavailable"
            answer: dict[str, Any] = {"id": ident, "ok": False, "error": code}
            detail = getattr(error, "detail", None)
            if detail:
                answer["detail"] = list(detail)[:32]
            return answer
        return {"id": ident, "ok": True, "result": result}


class _Connection:
    def __init__(self, path: str, timeout: float) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect(path)
        except OSError:
            self.socket.close()
            raise
        self.reader = self.socket.makefile("rb")

    def close(self) -> None:
        self.reader.close()
        self.socket.close()


class ApiClient:
    def __init__(self, path: str, timeout: float = 15.0) -> None:
        self.path = path
        self.timeout = timeout
        self._idle: list[_Connection] = []
        self._lock = threading.Lock()
        self._next = 0

    def _acquire(self, timeout: float) -> _Connection:
        with self._lock:
            connection = self._idle.pop() if self._idle else None
        if connection is None:
            connection = _Connection(self.path, timeout)
        connection.socket.settimeout(timeout)
        return connection

    def _release(self, connection: _Connection) -> None:
        with self._lock:
            if len(self._idle) < POOL_SIZE:
                self._idle.append(connection)
                return
        connection.close()

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()

    def call(self, op: str, args: Mapping[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        with self._lock:
            self._next += 1
            ident = self._next
        limit = self.timeout if timeout is None else timeout
        try:
            connection = self._acquire(limit)
        except OSError:
            raise ApiSockError("unavailable") from None
        try:
            connection.socket.sendall(encode({"id": ident, "op": op, "args": dict(args or {})}))
            line = connection.reader.readline(MAX_LINE + 1)
            answer = parse_json(line)
            if not isinstance(answer, dict) or answer.get("id") != ident:
                raise ValueError("answer does not match the request")
        except (OSError, ValueError):
            connection.close()
            raise ApiSockError("unavailable") from None
        self._release(connection)
        if answer.get("ok") is not True:
            raise ApiSockError(str(answer.get("error")), list(answer.get("detail", [])))
        return dict(answer["result"])

    def hello(self) -> dict[str, Any]:
        return self.call("hello")

    def resolve_session(self, token: str | None, peer_uid: int | None = None) -> dict[str, Any] | None:
        """The active session of a bearer token, or ``None``; only the token's SHA-256 leaves this process.

        ``peer_uid`` is the UID of the connecting process (``peer.peer_uid``; ``None``: binding off). A missing token
        is checked as the empty token, so it counts as a failed check too."""
        answer = self.call("session.resolve", {"token_sha256": token_sha256(token or ""), "peer_uid": peer_uid})
        retry = answer.get("retry_after")
        if type(retry) is int:
            raise AuthLimited(retry)
        session = answer["session"]
        return None if session is None else dict(session)

    def append(self, kind: str, body: Mapping[str, Any], session_id: str | None = None) -> int:
        return int(self.call("journal.append", {"kind": kind, "body": dict(body), "session_id": session_id})["seq"])

    def pending_create(self, session_id: str, kind: str, body: Mapping[str, Any]) -> str:
        result = self.call("pending.create", {"session_id": session_id, "kind": kind, "body": dict(body)})
        return str(result["pending_id"])

    def pending_wait(self, pending_id: str, session_id: str, timeout: float) -> dict[str, Any] | None:
        result = self.call(
            "pending.wait",
            {"pending_id": pending_id, "session_id": session_id, "timeout": timeout},
            timeout=timeout + WAIT_MARGIN,
        )
        return None if result["state"] == "waiting" else dict(result["answer"])
