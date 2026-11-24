"""Audit of `OPERATOR_GITHUB_USERS` (KOR-47, ADR-076 section 7; `contracts/audit/runner.operator_github_users.json`).

The users of that list connect through the operator's GitHub account and so receive its token. The daemon task
`github_operator_users` (`github/tasks.py`) runs once at each start: it compares the configured list with the list
audited last (`runner.operator_github_user`, migration 0018) and, when they differ, replaces the stored list and
writes one outbox item `audit` `runner.operator_github_users` with result `ok`, target `device:<device ID>` and the
detail `{count, added, removed}` (the size of the new list and the numbers of users added and removed) in the same
transaction, plus one log line `github_operator_users changed count=.. added=.. removed=..`. An unchanged list writes
nothing; a device that is not enrolled (no sync runtime) does nothing.

No user ID is stored or sent: each listed user is kept as HMAC-SHA256 under the device's storage key (hex), which is
enough to count additions and removals; without the key the rows cannot be matched to user IDs, unlike a plain hash
of an ID from the small set of known users. A first start counts every listed user as added (an empty list audits
nothing).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from agent_runner.orgs.db import session
from agent_runner.sync.runtime import runtime

ACTION = "runner.operator_github_users"


def _line(context: Any, text: str) -> None:
    context.log.write(f"github_operator_users {text}\n")
    context.log.flush()


def user_hmac(key: bytes, user: str) -> str:
    return hmac.new(key, user.encode("ascii"), hashlib.sha256).hexdigest()


async def audit(context: Any) -> None:
    """Compare, store and audit the configured list (module docstring)."""
    sync = runtime(context)
    if sync is None:
        return
    wanted = {user_hmac(sync.storage_key, user) for user in context.config.operator_github_users}
    async with session(context.pool) as conn, conn.transaction():
        cur = await conn.execute("SELECT user_hmac FROM runner.operator_github_user FOR UPDATE")
        stored = {str(row[0]) for row in await cur.fetchall()}
        added, removed = wanted - stored, stored - wanted
        if not added and not removed:
            return
        await conn.execute("DELETE FROM runner.operator_github_user WHERE user_hmac = ANY(%s)", (sorted(removed),))
        for item in sorted(added):
            await conn.execute("INSERT INTO runner.operator_github_user (user_hmac) VALUES (%s)", (item,))
        detail = {"count": len(wanted), "added": len(added), "removed": len(removed)}
        body = {
            "action": ACTION,
            "result": "ok",
            "target_kind": "device",
            "target_id": context.identity.device.device_id,
            "detail": detail,
        }
        await sync.enqueue(conn, "audit", body)
    _line(context, f"changed count={len(wanted)} added={len(added)} removed={len(removed)}")
