"""Retire hook of this area (`vm.extensions`, `<area>/vm_retire.py:RETIRE_HOOKS`; ADR-072 section 4, review KOR-33
no. 2): before a running VM is stopped or deleted the checkpoints of its main sessions are taken
(`checkpoint.Checkpointer.flush_vm`; their upload follows in the background), and a stopped VM whose sessions still
have a due checkpoint is not replaced by a clone of a newer golden (it is started again, the checkpoint is taken, and
the VM is replaced at a later start). `checkpoints` tells the switch to a newer golden (`vm.golden_switch`, ADR-093
section 1) which paused sessions have a complete newest checkpoint (`store.checkpoint_holds`), and `fallbacks` which
checkpoint a session that can never be checkpointed on its VM would continue from (`store.fallback_times`, HOT-24).
Without an enrolled device nothing happens and no paused session is vouched for."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from agent_runner.contexts import store
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.env import contexts_for
from agent_runner.vm.extensions import RetireHook
from agent_runner.vm.records import VmRow


async def flush(context: Any, row: VmRow) -> None:
    env = contexts_for(context)
    if env is not None:
        await Checkpointer(env).flush_vm(row.id)


async def pending(context: Any, row: VmRow) -> bool:
    env = contexts_for(context)
    return env is not None and await Checkpointer(env).vm_pending(row.id)


async def checkpoints(context: Any, row: VmRow, session_ids: list[str]) -> Mapping[str, str | None]:
    env = contexts_for(context)
    if env is None:
        return {}
    async with env.sessions.transaction() as conn:
        return await store.checkpoint_holds(conn, session_ids)


async def fallbacks(context: Any, row: VmRow, session_ids: list[str]) -> Mapping[str, datetime | None]:
    env = contexts_for(context)
    if env is None:
        return {}
    async with env.sessions.transaction() as conn:
        return await store.fallback_times(conn, session_ids)


RETIRE_HOOKS = [RetireHook("contexts.checkpoint", flush, pending, checkpoints, fallbacks)]
