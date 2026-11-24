"""Membership routes of the hub fake as the hub runs them (`agent_hub.orgs.append`, `agent_hub.invites`,
`agent_hub.workgroups.routes`, `agent_hub.me.routes`):

- `POST /v1/orgs/{org}/log` replaces the plain append of `hubfake.py`: entries only by the appender (`403
  forbidden`), no `invite`/`invite_revoke` on this route, `expected_seq` (`409 seq_conflict`), `check_batch` with the
  receipt time, the org's platform access and the decertified platform devices excluded (the rotation obligation R and
  the distribution of every new epoch), the events of the batch checked with `check_event_hub` against the log after
  it; on success the wraps of principals the batch removed are deleted for this org's scopes and the receipt time of
  every new epoch is kept (E6);
- `POST /v1/orgs/{org}/invites/{id}/revoke` (`invite_revoke` with its invitation row, admin);
- `GET /v1/orgs/{org}/devices` (`org:member`): the device members of the log with the presence of `Extra.online` and
  the VMs of `Extra.vms`;
- `GET /v1/me` names the devices holding a wrap of the user's current `CK` (`cred_holders`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.events import check_event_hub
from agent_runner.crypto.log.state import DEVICE, ORG, WG, OrgState
from agent_runner.crypto.log.wraps import CRED

from ..hubfake import HubFake, Json, Request, now_ms_text

CAPACITY = {
    "vm_memory_budget_mib": 8192,
    "vm_memory_used_mib": 0,
    "vm_slots_total": 4,
    "vm_slots_free": 4,
    "pool_ready": 1,
}


@dataclass
class Extra:
    """State of the stage-a routes kept on the hub fake."""

    rotated_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    online: dict[str, bool] = field(default_factory=dict)
    hub_devices: list[Json] = field(default_factory=list)
    """Further rows of `GET /v1/orgs/{org}/devices` (devices the log does not know)."""
    vms: dict[str, list[Json]] = field(default_factory=dict)
    """The VMs the hub reports per device (ADR-075)."""
    entries: dict[str, Json] = field(default_factory=dict)
    """Whiteboard entries by ID (`fakes/boards.py`)."""


def extra(hub: HubFake) -> Extra:
    found = getattr(hub, "stage_a", None)
    if found is None:
        found = Extra()
        setattr(hub, "stage_a", found)  # noqa: B010
    return cast(Extra, found)


def created_scopes(body: Json) -> list[tuple[str, str]]:
    if body["type"] == "wg_create":
        return [(WG, body["wg"])]
    if body["type"] == "rotate":
        return [(body["scope"], body["scope_id"])]
    return []


def in_org(state: OrgState, wrap: Json) -> bool:
    return (wrap["scope"] == ORG and wrap["scope_id"] == state.org) or (
        wrap["scope"] == WG and wrap["scope_id"] in state.workgroups
    )


def check_events(
    hub: HubFake, events: list[Any], log: Any, now: datetime, times: dict[tuple[str, str], datetime]
) -> None:
    skip = hub.platform_state().excluded(log.state)
    for event in events:
        header = event.get("header") if isinstance(event, dict) else None
        scope = (str(header.get("scope")), str(header.get("scope_id"))) if isinstance(header, dict) else ("", "")
        check_event_hub(event, log, now=now, rotated_at=times.get(scope), session_device=None, excluded=skip)


def install(hub: HubFake) -> None:
    state = extra(hub)
    original_me = hub.r_me

    def log_append(request: Request, body: Json, org: str) -> tuple[int, Any]:
        found = hub.orgs.get(org)
        if found is None:
            return 404, {"error": "not_found"}
        user = cast(str, request.user)
        entries = body["entries"]
        if any(entry["body"]["by"] != user for entry in entries):
            return 403, {"error": "forbidden"}
        if any(entry["body"]["type"] in ("invite", "invite_revoke") for entry in entries):
            return 400, {"error": "request_invalid"}
        if body["expected_seq"] != found.log.head.seq + 1:
            return 409, {"error": "seq_conflict", "detail": {"expected_seq": found.log.head.seq + 1}}
        now = datetime.now(UTC)
        platform = hub.platform_state()
        result = check_batch(
            found.log,
            entries,
            body["wraps"],
            wrapped_by=user,
            platform=platform,
            now=now,
            platform_access=found.platform_access or platform.platform_access(org, private=False),
            excluded=platform.excluded(found.log.state),
        )
        times = dict(state.rotated_at)
        times |= {scope: now for entry in entries for scope in created_scopes(entry["body"])}
        check_events(hub, body["events"], result.log, now, times)
        before = found.log.state
        found.log = result.log
        state.rotated_at = times
        removed = {p.id for p in before.principals.values() if p.active and found.log.state.member(p.id) is None}
        hub.wraps[:] = [w for w in hub.wraps if not (w["recipient_id"] in removed and in_org(before, w))]
        for wrap in body["wraps"]:
            hub.wraps.append({**wrap, "wrapped_by": user, "created_at": now_ms_text()})
        for event in body["events"]:
            hub.store_event(org, event)
        return 200, {"head": result.log.head.to_json()}

    def revoke(request: Request, body: Json, org: str, invite: str) -> tuple[int, Any]:
        found = hub.orgs.get(org)
        user = cast(str, request.user)
        if found is None or not found.log.state.is_admin(user):
            return 403, {"error": "forbidden"}
        entry = body["entry"]
        if entry["body"]["type"] != "invite_revoke" or entry["body"]["invite_id"] != invite:
            return 400, {"error": "request_invalid"}
        if invite not in found.log.state.invites:
            return 404, {"error": "not_found"}
        found.log = check_batch(found.log, [entry], [], wrapped_by=user).log
        hub.invites.pop(invite, None)
        return 200, {"seq": found.log.head.seq}

    def devices(request: Request, body: Any, org: str) -> tuple[int, Any]:
        found = hub.orgs.get(org)
        if found is None or found.log.state.member(cast(str, request.user)) is None:
            return 404, {"error": "not_found"}
        org_state = found.log.state
        rows = [
            {
                "device_id": p.id,
                "kind": "org" if p.platform_seq is None else "platform",
                "state": "active",
                "online": state.online.get(p.id, False),
                "last_seen_at": None,
                "agent_version": "1.0.0",
                "capacity": CAPACITY,
                "capacity_low": False,
                "assigned_wgs": org_state.device_wgs(p.id),
                "assigned_org": org_state.org_assigned(p.id),
                "vms": state.vms.get(p.id, []),
            }
            for p in org_state.principals.values()
            if p.kind == DEVICE and p.active
        ]
        return 200, {"devices": [*rows, *state.hub_devices]}

    def me(request: Request, body: Any) -> tuple[int, Any]:
        status, answer = original_me(request, body)
        user = hub.users[cast(str, request.user)]
        holders = {
            w["recipient_id"]
            for w in hub.wraps
            if (w["scope"], w["scope_id"], w["epoch"]) == (CRED, user.id, user.cred_epoch)
            and w["recipient_id"] != user.id
        }
        return status, {**answer, "cred_holders": {"epoch": user.cred_epoch, "devices": sorted(holders)}}

    hub.override("r_log_append", log_append)
    hub.override("r_me", me)
    hub.add_route("POST", r"/v1/orgs/(?P<org>[0-9a-f-]+)/invites/(?P<invite>[A-Za-z0-9_-]+)/revoke", revoke)
    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/devices", devices)
