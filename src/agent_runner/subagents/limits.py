"""The subagent limits of the platform, from the last desired state (ADR-088 section 2).

The hub keeps the subagent limits as settings (`AGENT_HUB_SUBAGENT_MAX_DEPTH`, `…_TOTAL`, `…_RUNTIME_SECONDS`; 0 = no
limit) and sends them in the section `limits` of `GET /v1/device/state` (`{"api": 1, "subagent_max_depth",
"subagent_max_running" (always 0 since ADR-075 addendum 2: no number of subagents running at once; read past),
"subagent_max_total", "subagent_max_runtime_seconds"}`). The runner keeps
no copy: it reads the section of the desired state of the last reconcile (`SessionEnv.desired`) for the pre-check of
`subagent.request`, the runtime end of children, `get_context.limits` and the harness context.

- `of(desired)` / `current(context)`: the limits, None when the hub sent no valid section (an older hub): then the
  runner does no local pre-check (the hub decides, check H4) and shows the limits as unknown.
- `depth_refusal`, `total_refusal`: the sentence of a refusal (`limit_exceeded`) that names the limit, its value and
  the setting; None within the limit, without limits or for a limit of 0.
- No refusal for room (HOT-32): a child runs in the VM of its task, which holds the task's work, so a child or a
  resumed child is never refused because that VM is full - that would refuse running work. The room for subagents is
  kept when a **new** task is placed: it goes to a VM only while one more task as large as the largest one there,
  subagents included, still fits (`vm.room`, 05-runner.md section 3.3.1).
- `denied_message(found)`: the sentence of a hub `denied` `limit_exceeded` of `subagent.request` with the current
  limits (the hub's answer does not name the limit).
- `values(found)`: `(depth, running, total, runtime)` for the harness line, None each when unknown; `running` is 0
  (no limit) whenever the limits are known.
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

SECTION = "limits"
SERVICE_ATTRIBUTE = "session_service"
"""`sessions.service.ATTRIBUTE` (not imported: the session start reads this module; a test compares them)."""
FIELDS = ("subagent_max_depth", "subagent_max_total", "subagent_max_runtime_seconds")
OLD_GUEST_MAX_DEPTH = 2
OLD_GUEST_MAX_PARALLEL = 3
OLD_GUEST_MAX_TOTAL = 10


@dataclass(frozen=True)
class SubagentLimits:
    """The settings of the hub; 0 = no limit. There is no number of subagents running at once (ADR-075 addendum 2): an
    older hub's `subagent_max_running` is read past, and no subagent is refused for the room of its task's VM (HOT-32:
    the placement of new tasks keeps the reserve, `vm.room`)."""

    max_depth: int
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
    depth, total, runtime = numbers
    if depth is None or total is None or runtime is None:
        return None
    return SubagentLimits(max_depth=depth, max_total=total, max_runtime_seconds=runtime)


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


def total_refusal(found: SubagentLimits | None, total: int) -> str | None:
    if found is None or not found.max_total or total < found.max_total:
        return None
    return (
        f"This task already started {_plural(found.max_total, 'subagent')}, the limit of this platform (setting"
        " subagent_max_total). Give further instructions to a subagent it has with subagent.instruct."
    )


def _phrases(found: SubagentLimits) -> list[str]:
    return [
        f"depth {found.max_depth}" if found.max_depth else "no depth limit",
        f"{found.max_total} per task" if found.max_total else "no limit per task",
        f"runtime at most {found.max_runtime_seconds} s" if found.max_runtime_seconds else "no runtime limit",
    ]


def denied_message(found: SubagentLimits | None) -> str:
    """The answer to a hub `denied` `limit_exceeded` of `subagent.request` (module docstring)."""
    current_values = "not known on this device" if found is None else ", ".join(_phrases(found))
    return (
        "A subagent limit of this platform is reached (settings subagent_max_depth,"
        f" subagent_max_total; current limits: {current_values}). Stop a subagent with subagent.stop or wait until"
        " one ends."
    )


def values(found: SubagentLimits | None) -> tuple[int | None, int | None, int | None, int | None]:
    if found is None:
        return None, None, None, None
    return found.max_depth, 0, found.max_total, found.max_runtime_seconds  # 0: no number running at once


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
