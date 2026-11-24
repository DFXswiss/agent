"""Reconcile step `workvm` (order 57: after the sessions and billing steps; KOR-43, ADR-078 sections 4 and 5).

On every desired state of the hub:

- **closed tasks**: a session listed with `task_closed: true` names a closed task; every VM of that task (task,
  session and run VMs) is deleted at once with the reason `task_closed`, and its test runs end `cancelled`.
- **credit**: on a platform device every running work VM of an organization whose credit is used up is stopped at
  once (`credit_exhausted`: no RAM, no runtime billed); running test runs end `cancelled` within one poll
  (`testrun.TestRuns`). The VM is deleted after the idle time unless the resumed session uses it again.

The hub is untrusted (00-overview.md section 3): it can only make the device give up its own VMs early.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.workvm import records
from agent_runner.workvm.manager import RUN_SCOPE, WorkVms
from agent_runner.workvm.service import existing, runs_of

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def closed_tasks(desired: Mapping[str, Any]) -> list[str]:
    """The tasks the desired state names closed (malformed entries are ignored), in order."""
    sessions = desired.get("sessions")
    found: list[str] = []
    for item in sessions if isinstance(sessions, list) else []:
        if not isinstance(item, Mapping) or item.get("task_closed") is not True:
            continue
        task_id = item.get("task_id")
        if isinstance(task_id, str) and _UUID_RE.fullmatch(task_id) and task_id not in found:
            found.append(task_id)
    return found


async def credit_pass(workvms: WorkVms) -> list[str]:
    """Stop the running work VMs of exhausted organizations (module docstring); returns their IDs."""
    stopped: list[str] = []
    for vm in await records.live(workvms.deps.db, "running"):
        if (
            vm.scope != RUN_SCOPE
            and workvms.deps.exhausted(vm.org_id)
            and await workvms.stop(vm, reason="credit_exhausted")
        ):
            stopped.append(vm.id)
    return stopped


async def step(run: Run) -> None:
    workvms = existing(run.context)
    if run.desired is None or workvms is None:
        return
    runs = runs_of(run.context, workvms)
    for task_id in closed_tasks(run.desired):
        runs.cancel(reason="task_closed", task_id=task_id)
        await workvms.close_task(task_id)
    await credit_pass(workvms)


STEPS = [ReconcileStep(57, "workvm", step)]
