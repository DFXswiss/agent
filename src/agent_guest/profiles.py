"""The rights profile of a session in the guest (ADR-094 section 3; ``sessions-create`` ``profile``).

The runner sends the session's effective profile ``{name, operations, reads}`` with ``POST /v1/sessions`` (null or
absent: no profile, every right). The guest is the third of three checks (hub, runner, guest) and never the only one:

* ``disallowed_tools(profile)``: the tools the agent is not offered at all (``--disallowedTools``): the work VM tools
  without ``workvm.request`` and the browser tools without ``browser.open`` among the allowed types, and the model's
  own ``WebSearch`` for the profile ``analysis`` (ADR-097 section 2: it holds metadata of several workgroups and needs
  no outside source);
* ``refusal(profile, op_type)``: the local error of an operation the profile does not allow (``rights_denied``); the
  control process marks such a request ``local_check: "rejected"`` when it records it (``control_routes/pending.py``),
  so the runner registers it as rejected and never executes it.

Pure module: no clock, no files.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_guest.policy import PLATFORM_PREFIX, PLATFORM_TOOLS, WEB_SEARCH_TOOL

TOOL_TYPES: Mapping[str, str] = {"workvm_": "workvm.request", "browser_": "browser.open"}
NO_WEB_SEARCH = frozenset({"analysis"})
"""Profiles whose sessions are not offered the model's own web search (ADR-097 section 2)."""


def operations(profile: Mapping[str, Any] | None) -> frozenset[str] | None:
    """The allowed operation types, None without a profile."""
    if profile is None:
        return None
    return frozenset(str(item) for item in profile.get("operations", ()))


def disallowed_tools(profile: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The tools the session is not offered (module docstring), sorted."""
    allowed = operations(profile)
    if allowed is None:
        return ()
    hidden = {
        PLATFORM_PREFIX + tool
        for tool in PLATFORM_TOOLS
        for prefix, op_type in TOOL_TYPES.items()
        if tool.startswith(prefix) and op_type not in allowed
    }
    if profile is not None and profile.get("name") in NO_WEB_SEARCH:
        hidden.add(WEB_SEARCH_TOOL)
    return tuple(sorted(hidden))


def refusal(profile: Mapping[str, Any] | None, op_type: object) -> dict[str, Any] | None:
    """The local error of an operation type the profile does not allow, None when it is allowed."""
    allowed = operations(profile)
    if allowed is None or op_type in allowed:
        return None
    name = profile.get("name") if profile is not None else None
    return {
        "code": "rights_denied",
        "message": f"This session works with the rights profile {name}, which does not allow this operation type.",
    }
