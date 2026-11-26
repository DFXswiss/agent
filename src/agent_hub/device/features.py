"""Capabilities a runner announces on its live channel (ADR-060 decision 6).

The runner sends the header `Agent-Features: <feature>[,<feature>...]` with the signed upgrade request of
`GET /v1/device/ws` (a header, not a query parameter: an older hub refuses unknown query parameters but ignores unknown
headers; the header is not signed, and a device that lies about it only harms itself). The hub replaces
`device.features` with the announced set on every connection, so a runner that was downgraded loses its features
again. Unknown or malformed tokens are dropped.

`log.admit`: the runner verifies membership logs with `admit` entries (and the platform log with `user_certify`). The
hub refuses an `admit` batch while an active device of the org has not announced it (`outdated_devices`,
`409 peer_outdated`), and a `device_assign` of such a device into an org whose log already holds an `admit`.

`log.org_assign` and `vm.choice` (ADR-075): the runner verifies assignments to the whole organization (the hub refuses
them while an active device of the org has not announced it, `agent_hub.orgs.append`) and chooses the VM of each
session itself (the hub then takes `session.vm_id` from the session transitions only).

`github.identities`: the runner applies GitHub identity choices and defaults (ADR-078 section 3). The hub refuses to
store a task or session choice or a default while an active device assigned to a workgroup of the org has not
announced it (`agent_hub.github.identities.require_feature`, `409 peer_outdated`).

`log.policy` (ADR-079): the runner verifies the platform-log entries `platform_policy` and `org_operator_subscriptions`
and the platform role `operator` (the hub refuses to append them while an active platform device has not announced it,
`agent_hub.platform.log`).

`agent.profiles` (KOR-51, ADR-094 section 3): the runner enforces rights profiles (operation types, the work VM and
browser tools, the reach of reads) from the section `agent_profiles` of the device state. The hub refuses to assign a
profile to a task while an active device assigned to a workgroup of the org has not announced it
(`agent_hub.profiles.routes`, `409 peer_outdated`).
`log.support` (ADR-096): the runner verifies the membership-log entries `support_grant`/`support_revoke` and the
platform role `support` (the hub refuses to append them while an active device - of the org, or for the platform log
of any kind - has not announced it: an older runner would stop at the unknown type or value).

`platform.analysis` (KOR-53, ADR-097 section 2): the runner answers the read tools `get_usage`, `list_audit`,
`list_workgroup_operations` and `get_metrics` from the device routes of the workgroup. The hub creates an analysis task
only while every active device assigned to a workgroup of the org has announced it and `agent.profiles`
(`agent_hub.analysis.request`, `409 peer_outdated`).

`prompt.withdraw` (KOR-46, ADR-083 section 3): the runner withdraws a pending prompt listed in `prompt_queue.withdrawn`
of `GET /v1/device/state` and reports `session.prompt_withdrawn`. The hub refuses to request a withdrawal from a device
that has not announced it (`409 peer_outdated`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import psycopg

Connection = psycopg.Connection[Any]
HEADER = "agent-features"
LOG_ADMIT = "log.admit"
LOG_ORG_ASSIGN = "log.org_assign"
"""The runner verifies `device_assign_org` / `device_unassign_org` (ADR-075 section 5)."""
LOG_POLICY = "log.policy"
"""The runner verifies the platform log with `platform_policy`, `org_operator_subscriptions` and the role `operator`
(ADR-079)."""
LOG_SUPPORT = "log.support"
"""The runner verifies the support release (ADR-096): `support_grant`/`support_revoke` and the role `support`."""
VM_CHOICE = "vm.choice"
"""The runner chooses the VM of each session and names it in `session_transition` (ADR-075 section 4)."""
GITHUB_IDENTITIES = "github.identities"
PROMPT_WITHDRAW = "prompt.withdraw"
AGENT_PROFILES = "agent.profiles"
PLATFORM_ANALYSIS = "platform.analysis"
MAX_FEATURES = 16
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")


def parse_features(value: str | None) -> tuple[str, ...]:
    """The well-formed, distinct feature tokens of the header value, sorted (at most `MAX_FEATURES`)."""
    tokens = {token.strip() for token in (value or "").split(",")}
    return tuple(sorted(token for token in tokens if _TOKEN_RE.fullmatch(token)))[:MAX_FEATURES]


def record_features(connection: Connection, device_id: object, features: Iterable[str]) -> None:
    """Replace the announced features of a device."""
    connection.execute(
        "UPDATE hub.device SET features = %s WHERE id = %s AND features IS DISTINCT FROM %s",
        (list(features), device_id, list(features)),
    )


def outdated_devices(connection: Connection, devices: Iterable[str], feature: str = LOG_ADMIT) -> list[str]:
    """The devices of `devices` that have not announced `feature`, sorted."""
    wanted = sorted(set(devices))
    if not wanted:
        return []
    rows = connection.execute(
        "SELECT id FROM hub.device WHERE id = ANY(%s::uuid[]) AND NOT (%s = ANY(features))", (wanted, feature)
    ).fetchall()
    return sorted(str(row[0]) for row in rows)
