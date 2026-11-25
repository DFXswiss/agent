"""Daemon task `guestlink` (05-runner.md section 1.1): after the startup reconciliation of the VM area the sessions
whose VM is gone fail (`SessionService.vms_lost`, ADR-086 section 3.7), then the journal loops of the running VMs
(`SessionService.run_journals`). The task ends at once on a device that is not enrolled.
"""

from __future__ import annotations

from typing import Any

from agent_runner.registry import TaskSpec
from agent_runner.sessions.service import service_for


async def run_guestlink(context: Any) -> None:
    service = service_for(context)
    if service is None:
        context.log.write("runner.guestlink not_enrolled\n")
        context.log.flush()
        return
    await service.vms_ready()
    await service.vms_lost()
    await service.run_journals(context.stopping)


TASKS = [TaskSpec("guestlink", run_guestlink)]
