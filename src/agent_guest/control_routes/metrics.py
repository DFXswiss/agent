"""``GET /v1/metrics`` of the control socket (03-api.md section 7.1, ``guest/metrics.response.json``).

The values come from ``agent_guest.metrics`` (one ``Collector`` per control process); a VM source that cannot be read
answers ``503 unavailable``. ``SESSION_EVENTS`` adds the egress counting chain of a session at ``sandbox_ready``, so
packets are counted from before the agent starts (``metrics.EgressCounters``); the ``nft`` calls run in a background
thread so the session start does not wait for them.
"""

from __future__ import annotations

import threading

from agent_guest import metrics
from agent_guest.control import ControlContext
from agent_guest.routing import Request, Response, Route, json_response


def read(context: ControlContext, request: Request) -> Response:
    return json_response(metrics.collector(context).collect(context))


def sandbox_ready(context: ControlContext, session_id: str) -> None:
    """Add the session's egress chain in a background thread: the session start never waits for ``nft``."""
    current = metrics.collector(context)
    worker = threading.Thread(
        target=current.sync_egress, args=(metrics.active_sessions(context),), name="agent-guest-egress", daemon=True
    )
    current.egress_thread = worker
    worker.start()


ROUTES = [Route("GET", "/v1/metrics", read, request_schema="guest/metrics.request.json", body="none")]
SESSION_EVENTS = {"sandbox_ready": sandbox_ready}
