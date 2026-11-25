"""The subagent limits of the platform, from the last desired state (ADR-088 section 2).

The hub keeps every subagent limit as one setting (`AGENT_HUB_SUBAGENT_MAX_DEPTH`, `…_RUNNING`, `…_TOTAL`,
`…_RUNTIME_SECONDS`; 0 = no limit) and sends them in the section `limits` of `GET /v1/device/state` (`{"api": 1,
"subagent_max_depth", "subagent_max_running", "subagent_max_total", "subagent_max_runtime_seconds"}`). The runner keeps
no copy: it reads the section of the desired state of the last reconcile (`SessionEnv.desired`) for the pre-check of
`subagent.request`, the runtime end of children, `get_context.limits` and the harness context.

- `of(desired)` / `current(context)`: the limits, None when the hub sent no valid section (an older hub): then the
  runner does no local pre-check (the hub decides, check H4) and shows the limits as unknown.
- `depth_refusal`, `running_refusal`, `total_refusal`: the sentence of a refusal (`limit_exceeded`) that names the
  limit, its value and the setting; None within the limit, without limits or for a limit of 0.
- `vm_room_refusal(context, conn, vm_id)`: the parent's VM already has `AGENT_RUNNER_SESSIONS_PER_VM` occupying
  sessions (`vm.records.ACTIVE_SESSION_STATES`, the placement's count; 0 = no limit), so a child (or a resumed child)
  would not fit.
- `denied_message(found)`: the sentence of a hub `denied` `limit_exceeded` of `subagent.request` with the current
  limits (the hub's answer does not name the limit).
- `values(found)`: `(depth, running, total, runtime)` for the harness line, None each when unknown.
- `context_limits(found, current_guest)`: `get_context.limits`; for a guest before ADR-088 (`guest_features`) the
  values are clamped into its former contract (depth at most 2, parallel at most 3, total at most 10, no nulls, no
  runtime; 0 or unknown is the former maximum), so the guest's own validation of the answer passes.
- `OLD_GUEST_MAX_DEPTH`: such a guest also refuses a session deeper than 2 (its `POST /v1/sessions` contract), so the
  pre-check refuses a deeper child on its VM.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner.vm.records import ACTIVE_SESSION_STATES

SECTION = "limits"
SERVICE_ATTRIBUTE = "session_service"
"""`sessions.service.ATTRIBUTE` (not imported: the session start reads this module; a test compares them)."""
FIELDS = ("subagent_max_depth", "subagent_max_running", "subagent_max_total", "subagent_max_runtime_seconds")
OLD_GUEST_MAX_DEPTH = 2
OLD_GUEST_MAX_PARALLEL = 3
OLD_GUEST_MAX_TOTAL = 10
VM_SETTING = "AGENT_RUNNER_SESSIONS_PER_VM"


@dataclass(frozen=True)
class SubagentLimits:
    """The four settings of the hub; 0 = no limit."""

    max_depth: int
    max_running: int
    max_total: int
    max_runtime_seconds: int


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def of(desired: Mapping[str, Any] | None) -> SubagentLimits | None:
    """The limits of the desired state; None without a valid section `limits` (an older hub)."""
    found = None if desired is None else desired.get(SECTION)
    if not isinstance(found, Mapping):
        return None
    api = found.get("api")
    numbers = [_count(found.get(name)) for name in FIELDS]
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        return None
    depth, running, total, runtime = numbers
    if depth is None or running is None or total is None or runtime is None:
        return None
    return SubagentLimits(max_depth=depth, max_running=running, max_total=total, max_runtime_seconds=runtime)


def current(context: Any) -> SubagentLimits | None:
    """The limits of the last desired state the session area keeps; None before the first reconcile or without them."""
    service = getattr(context, SERVICE_ATTRIBUTE, None)
    env = getattr(service, "env", None)
    return of(getattr(env, "desired", None))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def depth_refusal(found: SubagentLimits | None, parent_depth: int) -> str | None:
    if found is None or not found.max_depth or parent_depth + 1 <= found.max_depth:
        return None
    return (
        f"Subagents may be nested at most {_plural(found.max_depth, 'level')} deep, the limit of this platform"
        " (setting subagent_max_depth). Do this work in this session or let a subagent of a lower level request it."
    )


def old_guest_depth_refusal(parent_depth: int) -> str | None:
    """A child deeper than the former contract allows on a VM whose guest is older than ADR-088."""
    if parent_depth + 1 <= OLD_GUEST_MAX_DEPTH:
        return None
    return (
        f"Subagents may be nested at most {OLD_GUEST_MAX_DEPTH} levels deep on the computer of this task until it runs"
        " the current guest version. Do this work in this session."
    )


def running_refusal(found: SubagentLimits | None, running: int) -> str | None:
    if found is None or not found.max_running or running < found.max_running:
        return None
    return (
        f"This task already has {_plural(found.max_running, 'running subagent')}, the limit of this platform"
        " (setting subagent_max_running). Stop one with subagent.stop or wait until one ends."
    )


def total_refusal(found: SubagentLimits | None, total: int) -> str | None:
    if found is None or not found.max_total or total < found.max_total:
        return None
    return (
        f"This task already started {_plural(found.max_total, 'subagent')}, the limit of this platform (setting"
        " subagent_max_total). Give further instructions to a subagent it has with subagent.instruct."
    )


async def vm_room_refusal(context: Any, conn: AsyncConnection[Any], vm_id: str | None) -> str | None:
    """The sentence when the VM `vm_id` has no room for one more session (module docstring), else None."""
    if vm_id is None:
        return None
    limit = int(context.config.sessions_per_vm)
    if limit == 0:
        return None
    cursor = await conn.execute(
        "SELECT count(*) FROM runner.session WHERE vm_id = %s AND state = ANY(%s)", (vm_id, list(ACTIVE_SESSION_STATES))
    )
    row = await cursor.fetchone()
    assert row is not None
    occupied = int(row[0])
    if occupied < limit:
        return None
    return (
        f"The computer of this task is full: {_plural(occupied, 'session')} {'runs' if occupied == 1 else 'run'} in"
        " it, the limit of this device"
        f" (setting {VM_SETTING}). Stop a subagent with subagent.stop or wait until one ends."
    )


def _phrases(found: SubagentLimits) -> list[str]:
    return [
        f"depth {found.max_depth}" if found.max_depth else "no depth limit",
        f"{found.max_running} running at a time" if found.max_running else "no limit of running subagents",
        f"{found.max_total} per task" if found.max_total else "no limit per task",
        f"runtime at most {found.max_runtime_seconds} s" if found.max_runtime_seconds else "no runtime limit",
    ]


def denied_message(found: SubagentLimits | None) -> str:
    """The answer to a hub `denied` `limit_exceeded` of `subagent.request` (module docstring)."""
    current_values = "not known on this device" if found is None else ", ".join(_phrases(found))
    return (
        "A subagent limit of this platform is reached (settings subagent_max_depth, subagent_max_running,"
        f" subagent_max_total; current limits: {current_values}). Stop a subagent with subagent.stop or wait until"
        " one ends."
    )


def values(found: SubagentLimits | None) -> tuple[int | None, int | None, int | None, int | None]:
    if found is None:
        return None, None, None, None
    return found.max_depth, found.max_running, found.max_total, found.max_runtime_seconds


def _clamped(value: int | None, maximum: int) -> int:
    return maximum if not value else min(value, maximum)


def context_limits(found: SubagentLimits | None, current_guest: bool) -> dict[str, int | None]:
    """`get_context.limits` (module docstring)."""
    depth, running, total, runtime = values(found)
    if not current_guest:
        return {
            "subagent_max_depth": _clamped(depth, OLD_GUEST_MAX_DEPTH),
            "subagent_max_parallel": _clamped(running, OLD_GUEST_MAX_PARALLEL),
            "subagent_max_total": _clamped(total, OLD_GUEST_MAX_TOTAL),
        }
    return {
        "subagent_max_depth": depth,
        "subagent_max_parallel": running,
        "subagent_max_total": total,
        "subagent_max_runtime_seconds": runtime,
    }
