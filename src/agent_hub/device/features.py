"""Capabilities a runner announces on its live channel (ADR-060 decision 6).

The runner sends the header `Agent-Features: <feature>[,<feature>...]` with the signed upgrade request of
`GET /v1/device/ws` (a header, not a query parameter: an older hub refuses unknown query parameters but ignores unknown
headers; the header is not signed, and a device that lies about it only harms itself). The hub replaces
`device.features` with the announced set on every connection, so a runner that was downgraded loses its features
again. Unknown or malformed tokens are dropped.

`log.admit`: the runner verifies membership logs with `admit` entries (and the platform log with `user_certify`). The
hub refuses an `admit` batch while an active device of the org has not announced it (`outdated_devices`,
`409 peer_outdated`), and a `device_assign` of such a device into an org whose log already holds an `admit`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

import psycopg

Connection = psycopg.Connection[Any]
HEADER = "agent-features"
LOG_ADMIT = "log.admit"
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
