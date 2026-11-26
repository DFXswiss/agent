"""Support routes of the hub fake as the hub runs them (`agent_hub.support.routes`, `agent_hub.orgs.routes`; ADR-096):

- `GET /v1/orgs/{org}/support` (`org:member`): the active releases of the org's log with the position and time of
  their entry, and the persons of the support roster of the platform log with the names of `Support.names`;
- `GET /v1/orgs/{org}` (`org:member`): the org's head and `rotation_pending` as `Support.pending` keeps it (the hub
  marks released workgroups there after a person left support).

`make_support` certifies a user's identity in the platform log and grants it the role `support`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from ..hubfake import HubFake, Json, Request, User, now_ms_text
from .account_views import platform_entry


@dataclass
class Support:
    names: dict[str, str] = field(default_factory=dict)
    """The display names the operator stored for support persons."""
    pending: dict[str, list[Json]] = field(default_factory=dict)
    """`rotation_pending` per org."""


def support(hub: HubFake) -> Support:
    found = getattr(hub, "support_views", None)
    if found is None:
        found = Support()
        setattr(hub, "support_views", found)  # noqa: B010
    return cast(Support, found)


def make_support(hub: HubFake, user: User, name: str | None = None) -> None:
    """A `user_certify` of the user's keys and a `role_grant support` bound to it."""
    hub.anchor_user(user)
    platform_entry(hub, type="role_grant", subject=user.id, role="support")
    if name is not None:
        support(hub).names[user.id] = name


def end_support(hub: HubFake, user: User) -> None:
    platform_entry(hub, type="role_revoke", subject=user.id, role="support")


def install(hub: HubFake) -> None:
    found = support(hub)

    def member(request: Request, org: str) -> Any:
        stored = hub.orgs.get(org)
        if stored is None or stored.log.state.member(cast(str, request.user)) is None:
            return None
        return stored

    def org_support(request: Request, body: Any, org: str) -> tuple[int, Any]:
        stored = member(request, org)
        if stored is None:
            return 404, {"error": "not_found"}
        released = sorted(stored.log.state.support.items())
        roster = hub.platform_state().support_roster()
        return 200, {
            "releases": [
                {"wg_id": wg, "access": access, "granted_seq": seq, "granted_at": now_ms_text()}
                for wg, (access, seq) in released
            ],
            "people": [{"user_id": user, "name": found.names.get(user)} for user in sorted(roster)],
        }

    def org_view(request: Request, body: Any, org: str) -> tuple[int, Any]:
        stored = member(request, org)
        if stored is None:
            return 404, {"error": "not_found"}
        return 200, {
            "org_id": org,
            "log_head": stored.log.head.to_json(),
            "rotation_pending": found.pending.get(org, []),
        }

    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/support", org_support)
    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)", org_view)
