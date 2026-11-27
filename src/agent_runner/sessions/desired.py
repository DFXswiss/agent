"""Desired sessions of `GET /v1/device/state` (03-api.md section 6.1, `contracts/api/device-state.response.json`).

The hub is untrusted (00-overview.md section 3, B2): only the fields the session step needs are read and each is
checked; a malformed list fails the step (`wire.ResponseInvalid`). Whether the device may act on a session is decided
against the verified membership log by the step, never by this list. `resume_from`, `vm_id_pref` and `vm_new_allowed`
(ADR-075 section 4) are optional: absent, there is no previous session, no preferred VM, and a new VM is allowed. A
preferred VM is only a preference: the VM manager takes it only when it is a live VM of the session's workgroup.
"""

from __future__ import annotations

import re
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
_VM_ID_RE = re.compile(r"^vm-[0-9a-f]{8}$")


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
    resume_from: str | None = None
    vm_id_pref: str | None = None
    vm_new_allowed: bool = True
    model_required: bool = False
    """`model_id_pref` was asked for explicitly (the `model_id` of a `subagent.request`, never a field of the hub): only
    an access whose catalog allows that model is taken (`model.access.walk`, HOT-31)."""


def _text(value: object, what: str, allowed: frozenset[str] | None = None, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= maximum or (allowed is not None and value not in allowed):
        raise wire.ResponseInvalid(what)
    return value


def parse(item: object) -> Desired:
    data = wire.obj(item, "sessions")
    parent = data.get("parent_session_id")
    model = data.get("model_id_pref")
    depth = data.get("depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise wire.ResponseInvalid("sessions")
    resume = data.get("resume_from")
    vm = data.get("vm_id_pref")
    if vm is not None and (not isinstance(vm, str) or not _VM_ID_RE.fullmatch(vm)):
        raise wire.ResponseInvalid("sessions")
    new_allowed = data.get("vm_new_allowed", True)
    if not isinstance(new_allowed, bool):
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
        resume_from=None if resume is None else wire.uuid_text(resume, "sessions"),
        vm_id_pref=vm,
        vm_new_allowed=new_allowed,
    )


def desired_sessions(answer: Mapping[str, Any]) -> list[Desired]:
    return [parse(item) for item in wire.array(answer.get("sessions"), "sessions")]
