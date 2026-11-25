"""The section `subscriptions` of `GET /v1/device/state` (03-api.md section 6.1, ADR-090; ADR-027 section 4
`<area>/device_state.py:SECTION`): `{api: 1}` tells the runner that this hub accepts `quota.subscription_id` and the
subscription endpoints of section 6.1, so a runner sends them only to such a hub."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest

API = 1


def provide(_request: StateRequest) -> Mapping[str, Any]:
    """The field of this area."""
    return {"subscriptions": {"api": API}}


SECTION = Section(("subscriptions",), provide)
