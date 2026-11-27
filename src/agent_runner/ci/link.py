"""The local socket between the CI controller and the CI area of `agent-runner` (spec 16 section 17.1).

A Unix socket the runner creates (`serve`) with mode 0660 and the group of the controller's account, so only the
runner and the controller reach it; nothing listens on the network. One JSON object per line in both directions, at
most `LINE_MAX` bytes: a request `{"id": <int>, "op": <name>, ...}` is answered by `{"id": <int>, "ok": true, ...}` or
`{"id": <int>, "ok": false, "error": <code>}`. An unknown operation is `unknown_op`, a line that is no JSON object
`request_invalid` (answered with `id` 0); the handler raises `LinkRefused(code)` for a refusal and any other
exception becomes `internal` (logged by the area, never with the request's content).

`RunnerLink.call(op, **body)` opens one connection per call, so a restarted runner is reached again without state;
a refusal raises `LinkRefused`, a missing socket or broken connection `LinkDown`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

LINE_MAX = 65536
CALL_TIMEOUT = 900.0
"""A VM start may wait in the device's queue (`WORKVM_QUEUE_SECONDS`, 900 s by default)."""

Handler = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class LinkRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LinkDown(Exception):
    """The runner's socket is missing or the connection broke."""


def _line(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, separators=(",", ":")).encode() + b"\n"


async def _answer(handler: Handler, raw: bytes, failed: Callable[[str, BaseException], None]) -> dict[str, Any]:
    try:
        request = json.loads(raw)
    except ValueError:
        return {"id": 0, "ok": False, "error": "request_invalid"}
    ident = request.get("id") if isinstance(request, dict) else None
    op = request.get("op") if isinstance(request, dict) else None
    if not isinstance(ident, int) or isinstance(ident, bool) or not isinstance(op, str):
        return {"id": 0, "ok": False, "error": "request_invalid"}
    body = {key: value for key, value in request.items() if key not in ("id", "op")}
    try:
        result = await handler(op, body)
    except LinkRefused as refused:
        return {"id": ident, "ok": False, "error": refused.code}
    except Exception as error:  # the controller gets a code, the log the class
        failed(op, error)
        return {"id": ident, "ok": False, "error": "internal"}
    return {**result, "id": ident, "ok": True}


async def serve(
    path: Path, handler: Handler, *, gid: int | None, failed: Callable[[str, BaseException], None]
) -> asyncio.AbstractServer:
    """Listen on `path` (module docstring); an old socket file is replaced."""

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await reader.readline():
                writer.write(_line(await _answer(handler, raw, failed)))
                await writer.drain()
        except (ValueError, ConnectionError):
            pass  # a line above the limit or a peer that went away ends the connection
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    server = await asyncio.start_unix_server(connection, path=str(path), limit=LINE_MAX)
    os.chmod(path, 0o660)
    if gid is not None:
        os.chown(path, -1, gid)
    return server


class RunnerLink:
    def __init__(self, path: Path, *, timeout: float = CALL_TIMEOUT) -> None:
        self.path = path
        self.timeout = timeout
        self.next_id = 0

    async def call(self, op: str, **body: Any) -> dict[str, Any]:
        """One request (module docstring)."""
        self.next_id += 1
        ident = self.next_id
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path), limit=LINE_MAX)
        except OSError as error:
            raise LinkDown(type(error).__name__) from None
        try:
            writer.write(_line({**body, "id": ident, "op": op}))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), self.timeout)
        except (OSError, ValueError, TimeoutError) as error:
            raise LinkDown(type(error).__name__) from None
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        try:
            answer = json.loads(raw)
        except ValueError:
            raise LinkDown("answer_invalid") from None
        if not isinstance(answer, dict) or answer.get("id") != ident:
            raise LinkDown("answer_invalid")
        if answer.get("ok") is not True:
            raise LinkRefused(str(answer.get("error", "internal")))
        return answer
