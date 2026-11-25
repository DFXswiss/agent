"""Shutdown hook of the work VM area: the live pictures of the platform browser end and their screencast processes
are killed (docs/platform/spec/13-work-vm.md section 13.6). The VMs themselves go with the next start's
reconciliation (`restart`)."""

from __future__ import annotations

from typing import Any

from agent_runner.registry import ShutdownHook
from agent_runner.workvm.service import existing_browsers


async def stop_live(context: Any) -> None:
    browsers = existing_browsers(context)
    if browsers is not None and browsers.live is not None:
        await browsers.live.stop_all()


HOOKS = [ShutdownHook("workvm-browser-live", stop_live)]
