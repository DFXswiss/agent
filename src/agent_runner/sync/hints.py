"""Dispatch of WebSocket hints (03-api.md section 6.2, ADR-027 section 4).

A hint is a content-free JSON frame `{"type": ...}`; the runner always fetches the data by signed HTTP. Each frame is
handed to the handler registered for its `type` in `agent_runner/<area>/hints.py:HANDLERS`
(`registry.hint_handlers`). A frame that is not a JSON object with a known `type` is logged and dropped; a failing
handler is logged by exception type only (never its message) and the connection stays up.

The sync area registers `hello` itself: it compares the hub's time with the local clock and logs a skew of more
than 5 s (`agent-runner doctor` reports the same bound).
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from agent_runner.sync.http import parse_time

MAX_SKEW_S = 5
_TYPE_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_MS_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d{3}Z")


def _line(log: TextIO, text: str) -> None:
    log.write(f"runner.sync {text}\n")
    log.flush()


async def hello(context: Any, hint: Mapping[str, object]) -> None:
    """`hello {hub_time}`: log the clock skew when it exceeds `MAX_SKEW_S`."""
    match = _MS_TIME_RE.fullmatch(str(hint.get("hub_time", "")))
    hub = None if match is None else parse_time(match.group(1) + "Z")
    if hub is None:
        _line(context.log, "hello_invalid")
        return
    skew = round(context.sync_runtime.wall() - hub)
    if abs(skew) > MAX_SKEW_S:
        _line(context.log, f"clock_skew seconds={skew}")


HANDLERS: dict[str, Callable[..., Any]] = {"hello": hello}


def parse(message: str | bytes) -> dict[str, Any] | None:
    """The hint object of a text frame, None for anything else."""
    if not isinstance(message, str):
        return None
    try:
        data = json.loads(message)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("type"), str) or not _TYPE_RE.fullmatch(data["type"]):
        return None
    return data


async def dispatch(handlers: Mapping[str, Callable[..., Any]], context: Any, message: str | bytes, log: TextIO) -> bool:
    """Hand one frame to its handler; True when a handler ran without raising."""
    hint = parse(message)
    if hint is None:
        _line(log, "hint_invalid")
        return False
    handler = handlers.get(hint["type"])
    if handler is None:
        _line(log, f"hint_unknown type={hint['type']}")
        return False
    try:
        result = handler(context, hint)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:
        _line(log, f"hint_failed type={hint['type']} error={type(exc).__name__}")
        return False
    return True
