"""Pending platform-device enrollments for the operator (`GET /v1/platform/enrollments`, 03-api.md section 4.3,
04-crypto.md section 5.9).

A platform device enrolls with an invitation code (`POST /v1/device/enroll`, HUB-08): the hub stores the request in
`hub.device_enrollment` and the device waits in `pending_certification`. This module lists the enrollments that are
not certified yet **and verify**: the invitation is an uncertified `device_invite` of the verified platform log, the
device ID is not certified, and both `enroll_sig` (device identity) and `inv_sig` (`pk_inv` of that invitation) are
valid over the enrollment text (04-crypto.md section 3). Anything else is left out, so the operator never certifies a
forged request. The response also carries the platform log (contract change HUB-04): the operator CLI verifies it
against its own root key and builds the next entries from it.
"""

from __future__ import annotations

from typing import Any, cast

import psycopg

from agent_hub.crypto import enroll
from agent_hub.crypto.encoding import b64u_decode, format_time
from agent_hub.crypto.errors import CryptoError
from agent_hub.log.platform import CERTIFIED, PlatformState

MAX_ENROLLMENTS = 1000
SIGNED_FIELDS = ("device_id", "kind", "invite_id", "pub", "agent_version", "os", "arch", "capacity")


def _verified(request: object, state: PlatformState, invite_id: str, device_id: str) -> dict[str, object] | None:
    if not isinstance(request, dict) or set(request) != {*SIGNED_FIELDS, "enroll_sig", "inv_sig"}:
        return None
    invite = state.invites.get(invite_id)
    if (
        request["kind"] != "platform"
        or request["invite_id"] != invite_id
        or request["device_id"] != device_id
        or invite is None
        or invite.kind != "device"
        or invite.certified_seq is not None
        or state.status(device_id) == CERTIFIED
    ):
        return None
    try:
        enroll.verify_enrollment(request, b64u_decode(invite.pk_inv))
    except CryptoError:
        return None
    return cast(dict[str, object], request)


def pending(connection: psycopg.Connection[Any], state: PlatformState | None) -> list[dict[str, object]]:
    """The verified pending enrollments, oldest first (the order the operator certifies them in)."""
    if state is None:
        return []
    rows = connection.execute(
        "SELECT invite_id, device_id, request, created_at FROM hub.device_enrollment"
        " WHERE certified_at IS NULL ORDER BY created_at, invite_id LIMIT %s",
        (MAX_ENROLLMENTS,),
    ).fetchall()
    found: list[dict[str, object]] = []
    for invite_id, device_id, request, created_at in rows:
        verified = _verified(request, state, str(invite_id), str(device_id))
        if verified is None:
            continue
        found.append(
            {
                "invite_id": verified["invite_id"],
                "device_id": verified["device_id"],
                "pub": verified["pub"],
                "enroll_sig": verified["enroll_sig"],
                "inv_sig": verified["inv_sig"],
                "agent_version": verified["agent_version"],
                "os": verified["os"],
                "arch": verified["arch"],
                "capacity": verified["capacity"],
                "created_at": format_time(created_at),
            }
        )
    return found
