"""Whether the guest of a VM knows ADR-088 (full subagents and limits as settings), the names of KOR-52 or the
`known` export of FIX-02 (`KNOWN_EXPORT`).

A guest validates its answers against the contracts bundled with it and refuses an answer its contracts do not allow
(`request_invalid`: the tool call then times out). A guest before ADR-088 (golden with guest version below `CURRENT`)
still has the former contracts: `get_context.limits` with depth at most 2, parallel at most 3, total at most 10 and no
nulls; `get_subagent_result` without a result while `idle`; no `subagent.instruct` or `subagent.stop`. A guest before
`NAMES` knows no `agent` and no `children` in `get_context` (KOR-52, ADR-095 section 3; HOT-23). The runner reads the
guest version from the VM's golden record (`runner.vm.golden_version` -> `runner.golden.guest_version`).

- `current_guest(db_or_conn, vm_id, version=CURRENT)`: True when the VM's guest is at least `version` - also for no VM
  yet (`vm_id` None: a new VM is cloned from the current golden). A VM or golden the store does not know, or a version
  that does not read as `major.minor.patch`, counts as an older guest (its answers stay valid for both).
"""

from __future__ import annotations

import re
from typing import Any

from psycopg import AsyncConnection

CURRENT = (0, 3, 0)
"""The first guest version that knows ADR-088."""
NAMES = (0, 5, 0)
"""The first guest version that knows `agent` and `children` in `get_context` (KOR-52, ADR-095 section 3)."""
KNOWN_EXPORT = (0, 8, 0)
"""The first guest version whose `files/export` takes `known` and answers `omitted` (FIX-02)."""
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_QUERY = "SELECT g.guest_version FROM runner.vm v JOIN runner.golden g ON g.version = v.golden_version WHERE v.id = %s"


def knows(guest_version: str | None, version: tuple[int, int, int] = CURRENT) -> bool:
    """`guest_version` is at least `version`."""
    match = None if guest_version is None else _VERSION_RE.match(guest_version)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) >= version


async def _version(conn: AsyncConnection[Any], vm_id: str) -> str | None:
    cursor = await conn.execute(_QUERY, (vm_id,))
    row = await cursor.fetchone()
    return None if row is None else str(row[0])


async def current_guest(db_or_conn: Any, vm_id: str | None, version: tuple[int, int, int] = CURRENT) -> bool:
    """True when the guest of `vm_id` is at least `version` (module docstring); `db_or_conn` is a pool or a
    connection."""
    if vm_id is None:
        return True
    if isinstance(db_or_conn, AsyncConnection):
        return knows(await _version(db_or_conn, vm_id), version)
    async with db_or_conn.connection() as conn:
        return knows(await _version(conn, vm_id), version)
