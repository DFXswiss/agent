"""Extension point of the VM area (ADR-027 section 4 style, like `sessions.extensions`; KOR-33 review no. 2).

Discovered with `registry.discover` (areas in sorted order), so other areas add files and never edit this area:

- `<area>/vm_retire.py:RETIRE_HOOKS`: sequence of `RetireHook(name, flush, pending, checkpoints=None, fallbacks=None)`.
  `flush(context, row)` runs before a running workgroup VM is stopped (idle stop, LRU stop, unhealthy restart or
  stop) or deleted (its workgroup was removed); `Retire.flush` waits at most `FLUSH_WAIT_S` for all hooks together -
  a hook that takes longer is cancelled, a failing one is logged, and the VM goes away in either case (a hook never
  blocks the VM area). `pending(context, row) -> bool` is asked before a stopped VM is replaced by a clone of a newer
  golden: when any hook answers True the VM is restarted instead and replaced at a later start; a failing hook counts
  as False.
  `checkpoints(context, row, session_ids) -> {session_id: reason | None}` (optional, ADR-093 section 1) judges the
  paused main sessions of an outdated VM: None when the session's newest checkpoint is complete, else the hold reason
  (`checkpoint_due`, `checkpoint_missing`, `workspace_incomplete`). `Retire.checkpoints` answers for every session: a
  session no hook vouches for is held (the first reason a hook named, else `checkpoint_missing`); a failing hook is
  logged and vouches for nobody. Without such a hook no paused session ever moves (`vm.golden_switch`); the reason
  `checkpoint_impossible` (HOT-24) names a session that can never be checkpointed on this VM.
  `fallbacks(context, row, session_ids) -> {session_id: time | None}` (optional, HOT-24) says for such sessions when
  the checkpoint was taken that they would continue from after a switch by hand (None: they start fresh);
  `Retire.fallbacks` takes the first answer per session, a failing hook is logged and answers nothing.

KOR-33 registers `contexts.checkpoint` (`contexts/vm_retire.py`): the due context checkpoints of the VM's sessions
are taken before it goes away, and a stopped VM with due checkpoints is not replaced; KOR-70 adds its `checkpoints`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.vm.records import VmRow

FLUSH_WAIT_S = 120.0
_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")

Flush = Callable[[Any, VmRow], Awaitable[None]]
Pending = Callable[[Any, VmRow], Awaitable[bool]]
Checkpoints = Callable[[Any, VmRow, list[str]], Awaitable[Mapping[str, str | None]]]
Fallbacks = Callable[[Any, VmRow, list[str]], Awaitable[Mapping[str, datetime | None]]]
MISSING = "checkpoint_missing"
Log = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class RetireHook:
    name: str
    flush: Flush
    pending: Pending
    checkpoints: Checkpoints | None = None
    fallbacks: Fallbacks | None = None


def retire_hooks(package: str = registry.ROOT_PACKAGE) -> list[RetireHook]:
    found: list[RetireHook] = []
    seen: set[str] = set()
    for area, value in registry.discover("vm_retire", "RETIRE_HOOKS", package):
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise RunnerError("registry_invalid", f"{area}.vm_retire")
        for item in value:
            if (
                not isinstance(item, RetireHook)
                or not _NAME_RE.fullmatch(item.name)
                or not callable(item.flush)
                or not callable(item.pending)
                or (item.checkpoints is not None and not callable(item.checkpoints))
                or (item.fallbacks is not None and not callable(item.fallbacks))
            ):
                raise RunnerError("registry_invalid", f"{area}.vm_retire")
            if item.name in seen:
                raise RunnerError("registry_duplicate", item.name)
            seen.add(item.name)
            found.append(item)
    return found


def _no_log(_name: str, _fields: dict[str, Any]) -> None:
    return None


@dataclass
class Retire:
    """The hooks bound to a daemon context (see the module docstring); `Retire()` has none."""

    context: Any = None
    hooks: Sequence[RetireHook] = ()
    log: Log = _no_log
    wait_s: float = FLUSH_WAIT_S

    async def flush(self, row: VmRow) -> None:
        if not self.hooks:
            return
        calls = [asyncio.ensure_future(self._flush_one(hook, row)) for hook in self.hooks]
        _, late = await asyncio.wait(calls, timeout=self.wait_s)
        for call in late:
            call.cancel()
        await asyncio.gather(*late, return_exceptions=True)
        if late:
            self.log("vm.retire_timeout", {"vm_id": row.id, "hooks": len(late)})

    async def _flush_one(self, hook: RetireHook, row: VmRow) -> None:
        try:
            await hook.flush(self.context, row)
        except Exception as exc:
            self.log("vm.retire_failed", {"vm_id": row.id, "hook": hook.name, "error": type(exc).__name__})

    async def pending(self, row: VmRow) -> bool:
        for hook in self.hooks:
            try:
                if await hook.pending(self.context, row):
                    return True
            except Exception as exc:
                self.log("vm.retire_failed", {"vm_id": row.id, "hook": hook.name, "error": type(exc).__name__})
        return False

    async def checkpoints(self, row: VmRow, session_ids: list[str]) -> dict[str, str | None]:
        """Per paused session: None when a hook vouches for its checkpoint, else the hold reason (module docstring)."""
        found: dict[str, str | None] = {}
        for hook in self.hooks:
            if hook.checkpoints is None:
                continue
            try:
                answer = await hook.checkpoints(self.context, row, list(session_ids))
            except Exception as exc:
                self.log("vm.retire_failed", {"vm_id": row.id, "hook": hook.name, "error": type(exc).__name__})
                continue
            for session_id in session_ids:
                if session_id not in answer:
                    continue
                reason = answer[session_id]
                if reason is None or session_id not in found:
                    found[session_id] = reason  # a vouching hook wins, else the first reason named
        return {session_id: found.get(session_id, MISSING) for session_id in session_ids}

    async def fallbacks(self, row: VmRow, session_ids: list[str]) -> dict[str, datetime | None]:
        """Per session: the time of the checkpoint it would continue from after a move without its newest state
        (None: fresh), the first answer of a hook (module docstring)."""
        found: dict[str, datetime | None] = {}
        for hook in self.hooks:
            if hook.fallbacks is None:
                continue
            try:
                answer = await hook.fallbacks(self.context, row, list(session_ids))
            except Exception as exc:
                self.log("vm.retire_failed", {"vm_id": row.id, "hook": hook.name, "error": type(exc).__name__})
                continue
            for session_id in session_ids:
                if session_id not in found and session_id in answer:
                    found[session_id] = answer[session_id]
        return {session_id: found.get(session_id) for session_id in session_ids}
