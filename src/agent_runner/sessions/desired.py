"""Desired sessions of `GET /v1/device/state` (03-api.md section 6.1, `contracts/api/device-state.response.json`).

The hub is untrusted (00-overview.md section 3, B2): only the fields the session step needs are read and each is
checked; a malformed list fails the step (`wire.ResponseInvalid`). Whether the device may act on a session is decided
against the verified membership log by the step, never by this list.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner.reconcile import wire

SESSION_STATES = frozenset(
    {
        "requested",
        "vm_assigning",
        "vm_ready",
        "sandbox_ready",
        "agent_starting",
        "prompt_delivering",
        "working",
        "idle",
        "suspended",
        "resuming",
        "stopping",
        "stopped",
        "failed",
    }
)
PREFERENCES = frozenset({"auto", "subscription", "api_key", "platform"})


@dataclass(frozen=True)
class Desired:
    session_id: str
    task_id: str
    org_id: str
    wg_id: str
    state: str
    parent_session_id: str | None
    depth: int
    owner_id: str
    model_access_pref: str
    model_id_pref: str | None


def _text(value: object, what: str, allowed: frozenset[str] | None = None, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= maximum or (allowed is not None and value not in allowed):
        raise wire.ResponseInvalid(what)
    return value


def parse(item: object) -> Desired:
    data = wire.obj(item, "sessions")
    parent = data.get("parent_session_id")
    model = data.get("model_id_pref")
    depth = data.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 2:
        raise wire.ResponseInvalid("sessions")
    return Desired(
        session_id=wire.uuid_text(data.get("session_id"), "sessions"),
        task_id=wire.uuid_text(data.get("task_id"), "sessions"),
        org_id=wire.uuid_text(data.get("org_id"), "sessions"),
        wg_id=wire.uuid_text(data.get("wg_id"), "sessions"),
        state=_text(data.get("state"), "sessions", SESSION_STATES),
        parent_session_id=None if parent is None else wire.uuid_text(parent, "sessions"),
        depth=depth,
        owner_id=wire.uuid_text(data.get("owner_id"), "sessions"),
        model_access_pref=_text(data.get("model_access_pref"), "sessions", PREFERENCES),
        model_id_pref=None if model is None else _text(model, "sessions"),
    )


def desired_sessions(answer: Mapping[str, Any]) -> list[Desired]:
    return [parse(item) for item in wire.array(answer.get("sessions"), "sessions")]
