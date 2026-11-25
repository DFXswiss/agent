"""The sections `sessions`, `pending_prompts`, `delivered_prompt_event_ids` and `prompt_queue` of `GET /v1/device/state`
(03-api.md section 6.1, ADR-027 section 4: `<area>/device_state.py:SECTION`).

- `sessions`: every session placed on the device that has not ended (`stopped`/`failed` are left out: the runner
  stops local sessions that are not desired) in a workgroup the device is still assigned to, oldest first, with the
  owner's model preference (`user_settings`, default `auto`). A main session names
  - `resume_from`: the task's previous main session when it had ended (`stopped`/`failed`) by the time the new one
    was created, **on any device** (a session still `stopping` is never resumed from); a session a device refused
    for lack of capacity before it ever worked ran no agent and is skipped: the session before it counts (KOR-14);
  - `context`: the newest complete context checkpoint of the task's main sessions `{object_id, session_id,
    created_at, size}` (`agent_hub.contexts.store.newest`) - for the successor of a session lost with its device the
    checkpoint the hub pinned at the move while it is complete (`session.restore_object`, ADR-086 section 3) -, from
    which the runner restores the same conversation
    on whatever VM or device it starts the session (03-api.md section 4.6.3, ADR-072 sections 5 and 7). Old runners
    ignore the field; without it a new runner reports the emergency path.
- per session, only when set: the chosen VM of its task (`vm_id_pref`, ADR-075 section 6, while it is a live VM of the
  workgroup on this device; a subagent has none) and `vm_new_allowed: false` while the workgroup has reached
  `AGENT_HUB_VMS_PER_WG` live VMs on all devices (the runner then takes no new VM for it; absent means true) and
  `task_closed: true` while its task is closed (the runner deletes the task's work VMs at once, ADR-080 section 5);
- per session, always: `github_identity` / `task_github_identity`, the GitHub identities chosen for the session and its
  task (ADR-078 section 3, null without a choice); `fallback`, the owner's crossing rules of model access
  (`{own_to_dfx, operator_to_platform}`, defaults false/true without settings), and `model_consent`, the task's
  consent to DFX access (ADR-081 sections 1 and 5, KOR-50);
- `pending_prompts` / `delivered_prompt_event_ids`: the prompt routing of `agent_hub.sessions.prompts` for the main
  sessions in `sessions` (pending only while the session is not `stopping`).
- `prompt_queue` (KOR-46, ADR-083 sections 1 and 3), always sent: among `pending_prompts`, `via_agent` the prompts the
  hub received from their author's agent (the runner's sender line names it) and `withdrawn` those whose author asked
  to withdraw them and whose withdrawal the device has not confirmed yet (the runner withdraws a prompt that has not
  reached the guest and reports `session.prompt_withdrawn`). Its presence tells a runner that this hub takes
  `triggered_by` and `session.prompt_withdrawn`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_hub.contexts import store as contexts
from agent_hub.device.state import Section, StateRequest
from agent_hub.scheduler import load
from agent_hub.sessions import prompts
from agent_hub.sessions.derive import STOPPING


def _resume_from(request: StateRequest, task_id: str, session_id: str) -> str | None:
    row = request.connection.execute(
        "SELECT p.id, p.ended_at IS NOT NULL AND p.ended_at <= s.created_at"
        " FROM hub.session s JOIN hub.session p ON p.task_id = s.task_id AND p.depth = 0"
        " AND (p.created_at, p.id) < (s.created_at, s.id) WHERE s.id = %s AND s.task_id = %s"
        " AND (p.first_working_at IS NOT NULL OR p.state <> 'failed' OR p.state_reason <> 'capacity_exhausted')"
        " ORDER BY p.created_at DESC, p.id DESC LIMIT 1",
        (session_id, task_id),
    ).fetchone()
    return str(row[0]) if row is not None and row[1] else None


def _pinned(request: StateRequest, task_id: str, object_id: object) -> dict[str, Any] | None:
    """The checkpoint the hub pinned for a successor after a device failure while it is complete (ADR-086 section 3)."""
    if object_id is None:
        return None
    return contexts.complete_checkpoint(request.connection, task_id, str(object_id))


def _new_vm_allowed(request: StateRequest, wg_id: str, known: dict[str, bool]) -> bool:
    """`vm_new_allowed` of the workgroup (module docstring), once per workgroup."""
    if wg_id not in known:
        left = load.allowance(request.connection, wg_id)
        known[wg_id] = left is None or left > 0
    return known[wg_id]


def provide(request: StateRequest) -> Mapping[str, Any]:
    """The four fields for the device of `request` (module docstring)."""
    rows = request.connection.execute(
        "SELECT s.id, s.task_id, s.org_id, s.wg_id, s.state, s.parent_session_id, s.depth, s.owner_id,"
        " COALESCE(u.model_access, 'auto'), u.model_id,"
        " (SELECT v.id FROM hub.vm v WHERE v.id = t.placement_vm_id AND v.device_id = s.device_id"
        " AND v.wg_id = s.wg_id AND v.state <> 'deleted' AND s.depth = 0), s.github_identity, t.github_identity,"
        " t.state = 'closed', COALESCE(u.fallback_own_to_dfx, false), COALESCE(u.fallback_operator_to_platform, true),"
        " t.model_consent, s.restore_object"
        " FROM hub.session s JOIN hub.task t ON t.id = s.task_id"
        " LEFT JOIN hub.user_settings u ON u.user_id = s.owner_id"
        " WHERE s.device_id = %s AND s.state NOT IN ('stopped', 'failed') ORDER BY s.created_at, s.id",
        (request.device_id,),
    ).fetchall()
    sessions: list[dict[str, Any]] = []
    allowed: dict[str, bool] = {}
    for row in rows:
        if not request.view.reads_wg(row[3]):
            continue
        item: dict[str, Any] = {
            "session_id": str(row[0]),
            "task_id": str(row[1]),
            "org_id": str(row[2]),
            "wg_id": str(row[3]),
            "state": row[4],
            "parent_session_id": None if row[5] is None else str(row[5]),
            "depth": row[6],
            "owner_id": str(row[7]),
            "model_access_pref": row[8],
            "model_id_pref": row[9],
            "github_identity": None if row[11] is None else str(row[11]),
            "task_github_identity": None if row[12] is None else str(row[12]),
            "fallback": {"own_to_dfx": row[14], "operator_to_platform": row[15]},
            "model_consent": row[16],
        }
        if row[10] is not None:
            item["vm_id_pref"] = row[10]
        if not _new_vm_allowed(request, str(row[3]), allowed):
            item["vm_new_allowed"] = False
        if row[13]:
            item["task_closed"] = True
        if row[6] == 0:
            previous = _resume_from(request, item["task_id"], item["session_id"])
            if previous is not None:
                item["resume_from"] = previous
            checkpoint = _pinned(request, item["task_id"], row[17]) or contexts.newest(
                request.connection, item["task_id"]
            )
            if checkpoint is not None:
                item["context"] = checkpoint
        sessions.append(item)
    mains = [item for item in sessions if item["depth"] == 0]
    routed = prompts.routing(request.connection, sorted({item["task_id"] for item in mains}))
    pending: list[dict[str, Any]] = []
    delivered: dict[str, list[str]] = {}
    for item in mains:
        found = routed[item["session_id"]]
        delivered[item["session_id"]] = [p.event_id for p in found.delivered[-prompts.DELIVERED_KEEP :]]
        if item["state"] == STOPPING:
            continue
        pending.extend(
            {
                "task_id": item["task_id"],
                "session_id": item["session_id"],
                "scope_id": p.scope_id,
                "scope_seq": p.scope_seq,
                "event_id": p.event_id,
            }
            for p in found.pending
        )
    ids = [item["event_id"] for item in pending]
    agent, withdrawn = prompts.via_agent(request.connection, ids), prompts.withdrawing(request.connection, ids)
    return {
        "sessions": sessions,
        "pending_prompts": pending,
        "delivered_prompt_event_ids": delivered,
        "prompt_queue": {
            "via_agent": [event for event in ids if event in agent],
            "withdrawn": [event for event in ids if event in withdrawn],
        },
    }


SECTION = Section(fields=("sessions", "pending_prompts", "delivered_prompt_event_ids", "prompt_queue"), provide=provide)
