"""Shared parts of the read tools (`tools/<tool>.py`, 03-api.md section 8.1).

- `answered(read)`: runs a read and turns `ReadRefused` into the tool error of the outcome; the dispatcher
  (`subagents/journal.py`, ADR-038) records every outcome as `platform.read` event and `read.session.<tool>` audit.
- `arguments(call, allowed, required)`: the guest checked the arguments against the tool schema, but the VM is not
  trusted by the host (00-overview.md section 3, B4), so every tool checks them again (`params_invalid`).
- `store(context)`: a store session and the storage key for opening kept events.
- `display(name, fallback)`: a plaintext name as the contract allows it (1-256 characters); an object whose profile
  event the device does not hold (yet) is shown by its ID.
- `known_task(context, scope, task_id)`: the task belongs to the session's workgroup, as the hub says (`not_found`
  otherwise); a task ID named only by events is not enough (the hub projects task whiteboards of existing tasks only).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection

from agent_runner.orgs.db import session as store_session
from agent_runner.queries import device_api
from agent_runner.queries.rights import ReadRefused, ReadScope
from agent_runner.subagents.extensions import QueryCall, QueryOutcome, failure
from agent_runner.sync.runtime import runtime

NAME_MAX = 256
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PARAMS_INVALID = ReadRefused("params_invalid", "The arguments do not match the tool.")
TASK_NOT_FOUND = ReadRefused("not_found", "There is no such task in this workgroup.")
NO_STORE = ReadRefused("unavailable", "This device cannot read its local state right now. Try again later.")


async def answered(read: Callable[[], Awaitable[QueryOutcome]]) -> QueryOutcome:
    try:
        return await read()
    except ReadRefused as refused:
        return failure(refused.error, refused.message)


def arguments(call: QueryCall, allowed: frozenset[str], required: frozenset[str] = frozenset()) -> Mapping[str, Any]:
    found = call.arguments
    if not set(found) <= allowed or not required <= set(found):
        raise PARAMS_INVALID
    return found


def integer(found: Mapping[str, Any], name: str, low: int, high: int, default: int) -> int:
    value = found.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise PARAMS_INVALID
    return value


def uuid_argument(found: Mapping[str, Any], name: str) -> str:
    value = found.get(name)
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise PARAMS_INVALID
    return value


def display(name: str | None, fallback: str) -> str:
    if name is None:
        return fallback
    return name[:NAME_MAX]


@asynccontextmanager
async def store(context: Any) -> AsyncIterator[tuple[AsyncConnection[Any], bytes]]:
    sync = runtime(context)
    if sync is None:
        raise NO_STORE
    async with store_session(context.pool) as conn:
        yield conn, sync.storage_key


async def known_task(context: Any, scope: ReadScope, task_id: str) -> None:
    if await device_api.task(context, scope, task_id) is None:
        raise TASK_NOT_FOUND


def ok(result: object, count: int, *, target_kind: str = "session", target_id: str | None = None) -> QueryOutcome:
    return QueryOutcome(result=result, result_count=count, target_kind=target_kind, target_id=target_id)
