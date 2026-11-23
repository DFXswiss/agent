"""WebSocket hints that start a reconcile run (03-api.md section 6.2, 05-runner.md section 2.2).

`reconcile {}`, `log {org_id, seq}`, `wraps {}`, `session.start {session_id}` and `decertified {}` carry no content
and are never trusted: each only wakes the engine, which fetches and verifies everything by signed HTTP. `session.start`
is the hub's hint for a session just placed on this device (03-api.md section 4.6.1 step 3): the run fetches
`GET /v1/device/state` at once, whose desired sessions the session service starts; without it a session of a
workgroup whose log did not change waited for the next periodic run. A `decertified` hint is also logged; the
device stops acting once the verified platform log shows the `device_decertify` (or the hub refuses it).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.reconcile.engine import engine_for


async def wake(context: Any, _hint: Mapping[str, object]) -> None:
    engine = engine_for(context)
    if engine is not None:
        engine.request()


async def decertified(context: Any, hint: Mapping[str, object]) -> None:
    context.log.write("runner.reconcile hint=decertified\n")
    context.log.flush()
    await wake(context, hint)


HANDLERS: dict[str, Callable[..., Any]] = {
    "reconcile": wake,
    "log": wake,
    "wraps": wake,
    "session.start": wake,
    "decertified": decertified,
}
