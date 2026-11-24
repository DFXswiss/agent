"""The section `github_defaults` of `GET /v1/device/state` (03-api.md section 6.1, ADR-027 section 4, ADR-078
section 3): the default GitHub identities of the organizations and workgroups the device is assigned to
(`{scope, scope_id, identity_id}`, sorted by scope and scope ID). The runner keeps the last answer, so a restart
without the hub never falls back to another account."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest
from agent_hub.github.identities import device_defaults


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The field `github_defaults` for the device of `request`."""
    return {"github_defaults": device_defaults(request.connection, request.view.assigned())}


SECTION = Section(fields=("github_defaults",), provide=provide)
