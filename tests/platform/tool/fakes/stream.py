"""The live stream `GET /v1/stream` of the hub fake as the hub serves it to a watching browser (`agent_hub.stream.sse`,
03-api.md section 5, 13-work-vm.md section 13.6): a signed request of a member of `org`; a session watched with
`browser=` must be readable (`404 not_found` otherwise, a signed refusal).

The answer is unsigned server-sent events. A test sets `hub.stream_script`: a function that gets the request handler
and writes the whole answer itself (`frames` writes `200` and the given chunks, with pauses between them; anything
else, such as a broken answer, is written by the test). Without a script the route answers `404 not_found`.
`hub.stream_requests` keeps the query of every accepted stream.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from ..hubfake import HubFake, Request
from .sessions import _right, state_of

Script = Callable[[Any], None]


def sse(frame: str, data: object) -> bytes:
    """One frame `event: <type>\\ndata: <json>\\n\\n` as the hub writes it."""
    return b"event: " + frame.encode() + b"\ndata: " + json.dumps(data, separators=(",", ":")).encode() + b"\n\n"


def frames(*chunks: bytes | float) -> Script:
    """A `200` stream that writes the chunks in order (a number pauses that many seconds) and then ends."""

    def script(handler: Any) -> None:
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            for chunk in chunks:
                if isinstance(chunk, bytes):
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                else:
                    time.sleep(chunk)
        except OSError:
            return  # the reader went away

    return script


def install(hub: HubFake) -> None:
    hub.stream_script = None  # type: ignore[attr-defined]
    hub.stream_requests = []  # type: ignore[attr-defined]

    def stream(request: Request, body: Any) -> tuple[int, Any]:
        if hub._member(request, request.query.get("org", "")) is None:
            return 404, {"error": "not_found"}
        watched = state_of(hub).rows.get(request.query.get("browser", ""))
        if watched is None or _right(hub, request, watched["wg_id"], write=False) is not None:
            return 404, {"error": "not_found"}
        script = getattr(hub, "stream_script", None)
        if script is None:
            return 404, {"error": "not_found"}
        hub.stream_requests.append(dict(request.query))  # type: ignore[attr-defined]
        return 200, script

    hub.add_route("GET", r"/v1/stream", stream)
