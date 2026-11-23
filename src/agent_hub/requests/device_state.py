"""The section `requests` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4): the active device
requests addressed to the device - `pending` or `delivered`, not past their expiry, of users the device still serves -
oldest first, as `{request_id}`. The runner fetches each with `GET /v1/device/requests/{id}` and abandons local flows of
requests that are no longer listed (answered, expired or replaced)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.requests.store import active_for_device


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `requests` for the device of `request`."""
    return {"requests": active_for_device(request.connection, request.device_id, request.now)}


SECTION = Section(fields=("requests",), provide=provide)
