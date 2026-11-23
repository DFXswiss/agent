"""Hub checks of `admit` entries at append time and the notification of the admitted user (ADR-060 decision 3).

Besides the log rules (`agent_hub.log.verify`, `agent_hub.log.batch`), which every reader checks as well, the hub
checks what only it knows:

1. the admitted `pub` is the subject's registered identity and the subject is an active, not disabled user
   (`log_admit`);
2. every active device of the org (decertified platform devices left out) announced `log.admit` on its live channel
   (`409 peer_outdated` with `detail.devices`), so an older runner never meets an `admit` it would refuse; a
   `device_assign` of a device without the feature into an org whose log holds an `admit` is refused alike;
3. one notification `org.admitted` per admission for the admitted user (`ref` = the admitting admin), published after
   the commit with the other notices of the batch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

import psycopg

from agent_hub.device.features import outdated_devices
from agent_hub.errors import HubError
from agent_hub.log.state import DEVICE
from agent_hub.log.verify import VerifiedLog
from agent_hub.notifications.rules import COLUMNS, Notification, new_id, row
from agent_hub.orgs import logs

Connection = psycopg.Connection[Any]
Body = dict[str, Any]
KIND = "org.admitted"
MAX_DETAIL_DEVICES = 64


def _require_registered(connection: Connection, body: Body) -> None:
    subject = body["subject"]
    active = connection.execute(
        "SELECT 1 FROM hub.principal WHERE id = %s AND kind = 'user' AND disabled_at IS NULL", (subject,)
    ).fetchone()
    pub = cast(dict[str, str], body["pub"])
    if active is None or logs.identity_keys(connection, subject) != (pub["kem"], pub["sig"]):
        raise HubError("log_admit")


def _require_features(connection: Connection, devices: Sequence[str]) -> None:
    outdated = outdated_devices(connection, devices)
    if outdated:
        raise HubError("peer_outdated", detail={"devices": outdated[:MAX_DETAIL_DEVICES]})


def check_admits(
    connection: Connection, log: VerifiedLog, bodies: Sequence[Body], excluded: frozenset[str]
) -> list[Body]:
    """Checks 1 and 2 of the module docstring for the new entries `bodies` of `log`; the `admit` bodies."""
    admits = [body for body in bodies if body["type"] == "admit"]
    for body in admits:
        _require_registered(connection, body)
    state = log.state
    if admits:
        devices = [p.id for p in state.principals.values() if p.kind == DEVICE and p.active and p.id not in excluded]
        _require_features(connection, devices)
    assigned = [body["device"] for body in bodies if body["type"] == "device_assign"]
    if assigned and any(cast(Body, entry["body"])["type"] == "admit" for entry in log.entries):
        _require_features(connection, assigned)
    return admits


def admitted_notices(connection: Connection, org: UUID, admits: Sequence[Body], now: datetime) -> list[Notification]:
    """Check 3: one `org.admitted` row per admission."""
    notices: list[Notification] = []
    for body in admits:
        found = connection.execute(
            "INSERT INTO hub.notification (id, recipient_id, kind, org_id, wg_id, task_id, ref, created_at)"
            f" VALUES (%s, %s, %s, %s, NULL, NULL, %s, %s) RETURNING {COLUMNS}",
            (new_id(now), body["subject"], KIND, org, body["by"], now),
        ).fetchall()
        notices += [row(item) for item in found]
    return notices
