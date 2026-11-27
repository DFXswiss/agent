"""The working modes the hub sent, kept in the store (ADR-098 sections 3 and 4; section `task_modes` of
`GET /v1/device/state`; runner migration `0040_task_modes`).

- `store(conn, desired)` (reconcile step `task_modes`): replaces the stored modes by the section of the last desired
  state. A malformed section fails the step (`wire.ResponseInvalid`); a desired state without it (an older hub, which
  knows no modes) clears them - without the section no mode is on.
- `active(conn, task_id)`: the names of the modes that are on for the task, by name.
- `listed(conn)`: every stored mode `(task_id, mode, chosen_by, driver)` - what the drivers run.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.reconcile import wire

SECTION = "task_modes"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
WAYS = frozenset({"web", "tool", "sentence", "inherited"})


@dataclass(frozen=True)
class TaskMode:
    task_id: str
    mode: str
    chosen_by: str
    driver: Mapping[str, Any] | None


def _entry(value: object) -> dict[str, Any]:
    data = wire.obj(value, SECTION)
    mode, way, driver, changed = data.get("mode"), data.get("way"), data.get("driver"), data.get("changed_at")
    wire.seq(data.get("version"), SECTION, 1)
    wire.uuid_text(data.get("chosen_by"), SECTION)
    if driver is not None:
        wire.obj(driver, SECTION)
    if (
        not isinstance(mode, str)
        or NAME_RE.fullmatch(mode) is None
        or way not in WAYS
        or not isinstance(changed, str)
        or TIME_RE.fullmatch(changed) is None
    ):
        raise wire.ResponseInvalid(SECTION)
    return dict(data)


def _check(section: object) -> dict[str, list[dict[str, Any]]]:
    data = wire.obj(section, SECTION)
    api, tasks = data.get("api"), data.get("tasks")
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise wire.ResponseInvalid(SECTION)
    found: dict[str, list[dict[str, Any]]] = {}
    for task_id, entries in wire.obj(tasks, SECTION).items():
        wire.uuid_text(task_id, SECTION)
        found[task_id] = [_entry(entry) for entry in wire.array(entries, SECTION)]
    return found


async def store(conn: AsyncConnection[Any], desired: Mapping[str, Any]) -> None:
    """Keep the section of the desired state (module docstring), in the caller's transaction."""
    tasks = _check(desired[SECTION]) if SECTION in desired else {}
    await conn.execute("DELETE FROM runner.task_mode")
    for task_id, entries in tasks.items():
        for entry in entries:
            driver = entry.get("driver")
            await conn.execute(
                "INSERT INTO runner.task_mode (task_id, mode, version, chosen_by, way, changed_at, driver)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    task_id,
                    entry["mode"],
                    entry["version"],
                    entry["chosen_by"],
                    entry["way"],
                    entry["changed_at"],
                    None if driver is None else Jsonb(dict(driver)),
                ),
            )


async def active(conn: AsyncConnection[Any], task_id: str) -> list[str]:
    cursor = await conn.execute("SELECT mode FROM runner.task_mode WHERE task_id = %s ORDER BY mode", (task_id,))
    return [str(row[0]) for row in await cursor.fetchall()]


async def listed(conn: AsyncConnection[Any]) -> list[TaskMode]:
    cursor = await conn.execute(
        "SELECT task_id::text, mode, chosen_by::text, driver FROM runner.task_mode ORDER BY task_id, mode"
    )
    return [TaskMode(row[0], row[1], row[2], row[3]) for row in await cursor.fetchall()]
