"""What one device may read, from the hub's own verified logs (03-api.md section 3 `device:wg`, `device:org`).

`device_view` starts from the orgs in which the device is an active member (`org_member`) and confirms each with the
verified membership log of that org (`agent_hub.orgs.logs`): the device must be an active member there and must not be
excluded by the platform log (a platform device whose certification - the one its org keys come from - has ended,
04-crypto.md section 6.2). For every confirmed org the view knows

- the workgroups the device is assigned to (`device:wg`, the `wg` scopes it reads),
- whether it reads the org scope (`device:org`: assigned to at least one workgroup of the org, 04-crypto.md 4.7),
- the log head and the scopes of `org.rotation_pending` it reads.

The device routes, the pull and the device-state sections answer from this view, so they agree with the key
entitlements (W2) and the event rights (E5) the hub enforces elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.log.state import ORG, WG
from agent_hub.log.verify import VerifiedLog
from agent_hub.orgs import logs

Connection = psycopg.Connection[Any]


@dataclass(frozen=True)
class OrgView:
    """One org of the device."""

    org: logs.OrgRow
    log: VerifiedLog
    wgs: tuple[str, ...]
    """The workgroups the device is assigned to, sorted."""
    reads_org: bool

    @property
    def org_id(self) -> str:
        return str(self.org.id)

    def scopes(self) -> list[tuple[str, str]]:
        """`(scope, scope_id)` the device reads: the org scope (if it does) first, then its workgroups."""
        found = [(ORG, self.org_id)] if self.reads_org else []
        return found + [(WG, wg) for wg in self.wgs]

    def rotation_pending(self) -> list[str]:
        """The scope IDs of `org.rotation_pending` the device reads."""
        readable = set(self.scopes())
        return sorted(scope_id for scope, scope_id in self.org.rotation_pending if (scope, scope_id) in readable)


@dataclass(frozen=True)
class DeviceView:
    """The orgs of one device (sorted by org ID)."""

    device_id: str
    orgs: tuple[OrgView, ...]

    def org(self, org_id: UUID | str) -> OrgView | None:
        """The view of one org, None if the device is no active member of it."""
        return next((item for item in self.orgs if item.org_id == str(org_id)), None)

    def assigned(self) -> list[tuple[str, str]]:
        """`(org_id, wg_id)` of every assigned workgroup."""
        return [(item.org_id, wg) for item in self.orgs for wg in item.wgs]

    def scope(self, scope_id: UUID | str) -> tuple[str, str] | None:
        """`(scope, org_id)` of a readable scope, None for a scope the device does not read (or that is unknown)."""
        text = str(scope_id)
        for item in self.orgs:
            for scope, found in item.scopes():
                if found == text:
                    return scope, item.org_id
        return None

    def reads_wg(self, wg_id: UUID | str) -> bool:
        """`device:wg`: the device is assigned to the workgroup."""
        return any(str(wg_id) in item.wgs for item in self.orgs)


def device_view(connection: Connection, device_id: UUID | str, root_key: bytes) -> DeviceView:
    """The view of a device (module docstring)."""
    device = str(device_id)
    platform = logs.platform_state(connection, root_key)
    found: list[OrgView] = []
    for org in logs.member_orgs(connection, device):
        log = logs.verified_log(connection, org, platform)
        excluded = frozenset() if platform is None else platform.excluded(log.state)
        if log.state.member(device) is None or device in excluded:
            continue
        found.append(
            OrgView(
                org=org,
                log=log,
                wgs=tuple(log.state.device_wgs(device)),
                reads_org=log.state.can_read(device, ORG, str(org.id)),
            )
        )
    return DeviceView(device_id=device, orgs=tuple(found))
