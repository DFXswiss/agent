"""Extension point of the VM area (ADR-027 section 4 style, like `sessions.extensions`; KOR-33 review no. 2).

Discovered with `registry.discover` (areas in sorted order), so other areas add files and never edit this area:

- `<area>/vm_retire.py:RETIRE_HOOKS`: sequence of `RetireHook(name, flush, pending)`.
  `flush(context, row)` runs before a running workgroup VM is stopped (idle stop, LRU stop, unhealthy restart or
  stop) or deleted (its workgroup was removed); `Retire.flush` waits at most `FLUSH_WAIT_S` for all hooks together -
  a hook that takes longer is cancelled, a failing one is logged, and the VM goes away in either case (a hook never
  blocks the VM area). `pending(context, row) -> bool` is asked before a stopped VM is replaced by a clone of a newer
  golden: when any hook answers True the VM is restarted instead (like a VM with a `suspended` session) and replaced
  at a later start; a failing hook counts as False.

KOR-33 registers `contexts.checkpoint` (`contexts/vm_retire.py`): the due context checkpoints of the VM's sessions
are taken before it goes away, and a stopped VM with due checkpoints is not replaced.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.vm.records import VmRow

FLUSH_WAIT_S = 120.0
_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")

Flush = Callable[[Any, VmRow], Awaitable[None]]
Pending = Callable[[Any, VmRow], Awaitable[bool]]
Log = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class RetireHook:
    name: str
    flush: Flush
    pending: Pending


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
