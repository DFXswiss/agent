"""Daemon task `dfx_keys` (ADR-080 section 7): every `TICK_S` the issuer of a platform device answers the listed
requests and polls the listed keys that are due (`issuer.Issuer.tick`; each key keeps its own 60 s schedule). A failing
round is logged by error type and the loop goes on. The task ends at once on an org device and on a device that is not
enrolled.

Tests set `dfx_key_options` (`issuer.IssuerOptions`) on the context to inject the HTTP client factory.
"""

from __future__ import annotations

from typing import Any

from agent_runner.dfxkeys.issuer import issuer_for
from agent_runner.registry import TaskSpec

TICK_S = 5.0


async def run(context: Any) -> None:
    issuer = issuer_for(context)
    if issuer is None:
        return
    while not context.stopping.is_set():
        try:
            await issuer.tick(issuer.wall())
        except Exception as exc:
            issuer.line(f"tick_failed error={type(exc).__name__}")
        await context.host.sleep(TICK_S)


TASKS = [TaskSpec("dfx_keys", run)]
