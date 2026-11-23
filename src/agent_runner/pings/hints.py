"""WebSocket hint `ping {org_id, wg_id?, task_id?, level, entry_id}` (03-api.md section 6.2, 05-runner.md section 9).

The hint carries no ping ID and is never trusted: it only resolves its scope against the sessions this device runs
(org level: the sessions of the org; workgroup level: of the workgroup; task level: of the task; none that ended) and,
when one is in scope, wakes the reconcile engine, whose step 70 (`reconcile.py`) fetches the durable `pings` of
`GET /v1/device/state` by signed HTTP and sends them. A hint of the wrong shape or without a session in scope is
logged and dropped (the periodic reconcile still delivers whatever the hub lists).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.orgs.db import session as store_session
from agent_runner.pings.reconcile import ENDED
from agent_runner.pings.send import LEVELS, pinger_for
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import engine_for


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.pings {text}\n")
    context.log.flush()


def scope(hint: Mapping[str, object]) -> tuple[str, str, str]:
    """`(level, session column, ID)` of a well-formed hint (`wire.ResponseInvalid` otherwise)."""
    level = hint.get("level")
    if not isinstance(level, str) or level not in LEVELS:
        raise wire.ResponseInvalid("hint")
    org_id = wire.uuid_text(hint.get("org_id"), "hint")
    wire.uuid_text(hint.get("entry_id"), "hint")
    if level == "org":
        return level, "org_id", org_id
    wg_id = wire.uuid_text(hint.get("wg_id"), "hint")
    if level == "workgroup":
        return level, "wg_id", wg_id
    return level, "task_id", wire.uuid_text(hint.get("task_id"), "hint")


async def in_scope(context: Any, column: str, value: str) -> list[str]:
    """The sessions of this device in the hint's scope that have not ended."""
    pinger = pinger_for(context)
    if pinger is None:
        return []
    async with store_session(pinger.env.db) as conn:
        rows = await (
            await conn.execute(
                f"SELECT id::text FROM runner.session WHERE {column} = %s AND state <> ALL(%s) ORDER BY id",
                (value, sorted(ENDED)),
            )
        ).fetchall()
    return [row[0] for row in rows]


async def ping(context: Any, hint: Mapping[str, object]) -> None:
    try:
        level, column, value = scope(hint)
    except wire.ResponseInvalid:
        _line(context, "hint_invalid type=ping")
        return
    sessions = await in_scope(context, column, value)
    if not sessions:
        _line(context, f"hint_out_of_scope level={level}")
        return
    engine = engine_for(context)
    if engine is not None:
        engine.request()
    _line(context, f"hint level={level} sessions={len(sessions)}")


HANDLERS: dict[str, Callable[..., Any]] = {"ping": ping}
