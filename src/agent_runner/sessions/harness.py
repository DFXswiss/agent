"""Harness context and resource limits of a session start (05-runner.md sections 4.3, 8 and 13; 03-api.md section 7.1).

The guest renders the harness file from its static rules and the `harness.context_markdown` of `POST /v1/sessions`:
the confidential part only the runner knows (org, workgroup and task names and IDs, task owner display name, the
owner's rights in the workgroup, subagent limits). `context_markdown` produces exactly the block that
`agent_guest.harness.context_markdown` documents (the guest package runs in the VM, so the format is copied here; a
test keeps both equal). Names come from the decrypted profile events (`profiles.names`); a name the device has not
received yet is shown as `unknown` - the start never waits for it.

Rights: the owner's access to the workgroup in the verified membership log (`write`, `read`); an owner without
access has `none`.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_runner.crypto.log.state import OrgState
from agent_runner.sessions.profiles import Names

UNKNOWN = "unknown"
SUBAGENT_LIMITS = (2, 3, 10)
"""Subagent depth, running children per task, children per task (05-runner.md section 13, ADR-017)."""
GIB = 1024**3


@dataclass(frozen=True)
class Limits:
    memory_max: int
    cpu_quota: int
    tasks_max: int
    runtime_max_sec: int

    def body(self) -> dict[str, int]:
        return {
            "memory_max": self.memory_max,
            "cpu_quota": self.cpu_quota,
            "tasks_max": self.tasks_max,
            "runtime_max_sec": self.runtime_max_sec,
        }


MAIN_LIMITS = Limits(memory_max=4 * GIB, cpu_quota=200, tasks_max=256, runtime_max_sec=8 * 3600)
"""05-runner.md section 4.3: `MemoryMax=4G`, `CPUQuota=200%`, `TasksMax=256`, `RuntimeMaxSec=8h` (main sessions)."""


def inline(value: str) -> str:
    """One line of text (copy of `agent_guest.harness.inline`)."""
    return " ".join(value.replace("`", "'").split())


def context_markdown(
    *,
    org: tuple[str, str],
    workgroup: tuple[str, str, bool],
    task: tuple[str, str],
    owner: str,
    rights: str,
    limits: tuple[int, int, int],
) -> str:
    """The context block (copy of `agent_guest.harness.context_markdown`)."""
    wg_id, wg_name, is_default = workgroup
    default = " (default workgroup)" if is_default else ""
    depth, parallel, total = limits
    return "\n".join(
        [
            f"- Organization: {inline(org[1])} (`{inline(org[0])}`)",
            f"- Workgroup: {inline(wg_name)}{default} (`{inline(wg_id)}`)",
            f"- Task: {inline(task[1])} (`{inline(task[0])}`)",
            f"- Task owner: {inline(owner)}",
            f"- Rights of the task owner in the workgroup: {inline(rights)}",
            f"- Subagent limits: depth {depth}, {parallel} running at a time, {total} per task",
        ]
    )


def rights(state: OrgState | None, owner_id: str, wg_id: str) -> str:
    """The owner's explicit access; an admin's inherited read does not reach a session (ADR-058 section 1)."""
    access = None if state is None else state.explicit_access(owner_id, wg_id)
    return access if access in ("read", "write") else "none"


def is_default(state: OrgState | None, wg_id: str) -> bool:
    return state is not None and state.default_wg == wg_id


def session_context(
    *, names: Names, state: OrgState | None, org_id: str, wg_id: str, task_id: str, owner_id: str
) -> str:
    return context_markdown(
        org=(org_id, names.org or UNKNOWN),
        workgroup=(wg_id, names.workgroup or UNKNOWN, is_default(state, wg_id)),
        task=(task_id, names.task or UNKNOWN),
        owner=names.owner or UNKNOWN,
        rights=rights(state, owner_id, wg_id),
        limits=SUBAGENT_LIMITS,
    )
