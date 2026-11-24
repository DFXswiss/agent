"""The section `credentials_changed` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4, ADR-035):
the owners whose credentials changed since the device last read them and for whom it is still eligible, sorted
(`agent_hub.credentials.notices`). The runner re-reads its cached records of those owners (and purges them on `404`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.credentials.notices import owners_for
from agent_hub.device.state import Section, StateRequest


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `credentials_changed` for the device of `request`."""
    return {"credentials_changed": owners_for(request.connection, request.device_id, request.config.platform_root_key)}


SECTION = Section(fields=("credentials_changed",), provide=provide)
