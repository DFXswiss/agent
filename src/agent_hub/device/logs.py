"""Log reads of a device (03-api.md section 6.1): `GET /v1/device/logs` and `GET /v1/device/platform-log`.

- `org_log_page`: the membership-log entries of one org after `after` (ascending, at most `limit`), with the head and
  `next_after`; only for an org in which the device is an active member (`agent_hub.device.access`); any other org,
  known or not, is `404 not_found`. The runner verifies the entries itself (04-crypto.md section 6.1).
- `platform_log_page`: the platform-log entries in the same form; `404 not_found` while the platform log is empty.
  The runner verifies them against its configured root key (section 6.2).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from agent_hub.device.access import DeviceView
from agent_hub.errors import HubError
from agent_hub.orgs import logs
from agent_hub.platform import log as platform_log

Connection = psycopg.Connection[Any]
PAGE_DEFAULT = 200
PAGE_MAX = 1000


def _page(items: list[dict[str, Any]], head: dict[str, Any], limit: int) -> dict[str, Any]:
    page = items[:limit]
    more = len(items) > limit
    return {"entries": page, "head": head, "next_after": page[-1]["seq"] if more else None}


def org_log_page(connection: Connection, view: DeviceView, org_id: UUID, after: int, limit: int) -> dict[str, Any]:
    """The membership-log page of an org of the device."""
    found = view.org(org_id)
    if found is None:
        raise HubError("not_found")
    org = found.org
    entries = logs.entries(connection, org.id, after, limit + 1)
    items = [{"seq": entry["body"]["seq"], "entry": entry} for entry in entries]
    return _page(items, {"seq": org.head_seq, "hash": org.head_hash}, limit)


def platform_log_page(connection: Connection, after: int, limit: int) -> dict[str, Any]:
    """The platform-log page."""
    head = platform_log.stored_head(connection)
    if head is None:
        raise HubError("not_found")
    items = [{"seq": seq, "entry": entry} for seq, entry in platform_log.stored_entries(connection, after, limit + 1)]
    return _page(items, head.to_json(), limit)
