"""Test support of the modes suites (KOR-75): IDs, a connection runner and stored sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from psycopg import AsyncConnection

from agent_runner.sessions import records as session_records

WG = "0193000c-0000-7000-8000-000000000001"
TASK = "0193000c-0000-7000-8000-000000000002"
ORG = "0193000c-0000-7000-8000-000000000003"
OWNER = "0193000c-0000-7000-8000-000000000004"
CHOOSER = "0193000c-0000-7000-8000-000000000005"
OTHER_TASK = "0193000c-0000-7000-8000-000000000006"
MAIN = "0193000c-0000-7000-8000-000000000010"
CHILD = "0193000c-0000-7000-8000-000000000011"
CHANGED = "2026-11-26T10:00:00.000Z"


def entry(mode: Any = "test", driver: Any = None, **extra: Any) -> dict[str, Any]:
    """One entry of the section `task_modes`; `mode` and `driver` take malformed values too (the refusal tests)."""
    return {
        "mode": mode,
        "version": 1,
        "chosen_by": CHOOSER,
        "way": "web",
        "changed_at": CHANGED,
        "driver": driver,
        **extra,
    }


def section(tasks: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    return {"task_modes": {"api": 1, "tasks": tasks if tasks is not None else {TASK: [entry()]}}}


def with_conn(dsn: str, body: Callable[[AsyncConnection[Any]], Awaitable[None]]) -> None:
    async def main() -> None:
        async with await AsyncConnection.connect(dsn, autocommit=True) as conn:
            await body(conn)

    asyncio.run(main())


async def session(
    conn: AsyncConnection[Any], sid: str = MAIN, *, task: str = TASK, depth: int = 0, vm: str | None = "vm-00000001"
) -> None:
    await session_records.insert(
        conn,
        session_id=sid,
        task_id=task,
        org_id=ORG,
        wg_id=WG,
        parent_id=None if depth == 0 else MAIN,
        depth=depth,
        owner_id=OWNER,
        created_at=datetime(2026, 11, 25, tzinfo=UTC),
    )
    if vm is not None:
        await session_records.update(conn, sid, vm_id=vm)
