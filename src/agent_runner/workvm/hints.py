"""WebSocket hint `browser {session_id}` (`<area>/hints.py:HANDLERS`; 03-api.md section 6.2,
docs/platform/spec/13-work-vm.md section 13.6): someone watches the session's platform browser, so the runner starts its
live picture (`browser_live.LiveStreams.start`) unless it already streams it. The hint carries no content and is never
trusted: a session without an open browser session on this device (or a malformed hint) is ignored, and a stream ends by
itself at most 10 s after the hub's lease ended (`409 not_watched`)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.workvm.service import browsers_for

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


async def watch(context: Any, hint: Mapping[str, object]) -> None:
    session_id = hint.get("session_id")
    if not isinstance(session_id, str) or _UUID_RE.fullmatch(session_id) is None:
        return
    browsers = browsers_for(context)
    if browsers is not None and browsers.live is not None:
        await browsers.live.start(session_id)


HANDLERS: dict[str, Callable[..., Any]] = {"browser": watch}
