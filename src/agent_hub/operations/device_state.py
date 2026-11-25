"""The section `limits` of `GET /v1/device/state` (ADR-088 section 2; extension point `<area>/device_state.py:SECTION`).

`{"api": 1, "subagent_max_depth", "subagent_max_running", "subagent_max_total", "subagent_max_runtime_seconds"}` -
the subagent limits of this hub (`AGENT_HUB_SUBAGENT_*`, 0 = no limit). It is the one source of the runner: its
pre-check of `subagent.request`, the runtime end of children, `get_context.limits` and the harness context. A runner
that gets no section (an older hub) does no local pre-check and shows the limits as unknown.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.device.state import Section, StateRequest

API = 1


def provide(request: StateRequest) -> Mapping[str, Any]:
    return {"limits": {"api": API, **request.config.subagent_limits.published()}}


SECTION = Section(("limits",), provide)
