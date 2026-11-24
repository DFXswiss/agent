"""The accounts area of the hub fake beyond the identity routes of `hubfake.py` as the hub runs it
(`agent_hub.users.routes`, `agent_hub.users.identity.invitees`; ADR-060):

- `GET /v1/me/invitees`: the accounts registered through the caller's own platform invitations, newest registration
  first, disabled accounts left out, each `{user_id, invite_id, registered_at, identity}` with the admissible identity
  of `GET /v1/users/{user}/identity` or null while the account is not anchored.

`add_invitee` records that `user` registered through a platform invitation of `inviter`.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from ..hubfake import HubFake, Json, Request, User, now_ms_text


@dataclass
class Registered:
    inviter: str
    user: str
    invite_id: str
    registered_at: str


@dataclass
class Admission:
    registered: list[Registered] = field(default_factory=list)


def state_of(hub: HubFake) -> Admission:
    found = getattr(hub, "admission", None)
    if found is None:
        found = Admission()
        setattr(hub, "admission", found)  # noqa: B010
    return cast(Admission, found)


def add_invitee(hub: HubFake, inviter: User, user: User, minutes_ago: int = 0) -> None:
    state_of(hub).registered.append(
        Registered(inviter.id, user.id, secrets.token_urlsafe(16), now_ms_text(-timedelta(minutes=minutes_ago)))
    )


def install(hub: HubFake) -> None:
    found = state_of(hub)

    def invitees(request: Request, body: Any) -> tuple[int, Any]:
        rows: list[Json] = []
        mine = [row for row in found.registered if row.inviter == request.user and not hub.users[row.user].disabled]
        mine.sort(key=lambda row: row.invite_id)
        for row in sorted(mine, key=lambda row: row.registered_at, reverse=True):
            user = hub.users[row.user]
            identity = None if user.anchor is None else hub.identity_answer(user)
            rows.append(
                {
                    "user_id": row.user,
                    "invite_id": row.invite_id,
                    "registered_at": row.registered_at,
                    "identity": identity,
                }
            )
        return 200, {"invitees": rows}

    hub.add_route("GET", r"/v1/me/invitees", invitees)
