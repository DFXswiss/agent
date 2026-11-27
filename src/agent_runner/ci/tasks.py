"""Daemon task `ci` (spec 16 section 17.4): the CI area's socket for the CI controller.

The area is switched on per device by the file `<HOME>/ci/area.json` (`agent-ci install` of the operator writes it):
`{"socket": "<absolute path>", "gid": <group of the controller's account>}`. Without the file, on a device without work
VMs or before enrollment the task ends at once; a file that does not fit is a log line and the task ends too. The
socket lives while the daemon runs; at shutdown the server closes and the area stops following its runner processes
(the VMs go with the work VM reconciliation of the next start).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_runner.ci.area import Area
from agent_runner.ci.link import serve
from agent_runner.registry import TaskSpec
from agent_runner.workvm.service import browsers_of, workvms_for

AREA_FILE = Path("ci") / "area.json"


def settings(home: Path) -> tuple[Path, int] | None:
    """`(socket, gid)` of `<home>/ci/area.json`, None when it is missing or does not fit."""
    try:
        data = json.loads((home / AREA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    socket, gid = (data.get("socket"), data.get("gid")) if isinstance(data, dict) else (None, None)
    if not isinstance(socket, str) or not socket.startswith("/") or "\x00" in socket:
        return None
    if not isinstance(gid, int) or isinstance(gid, bool) or gid < 0:
        return None
    return Path(socket), gid


def _line(context: Any, text: str) -> None:
    context.log.write(text + "\n")
    context.log.flush()


async def run_ci(context: Any) -> None:
    found = settings(context.config.home)
    if found is None:
        return
    workvms = workvms_for(context)
    if workvms is None:
        _line(context, "runner.ci off reason=no_workvms")
        return
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points

    service = service_for(context)
    if service is None:
        return
    env = service.env

    async def enqueue_audit(item: Mapping[str, Any]) -> None:
        async with env.transaction() as conn:
            await env.outbox.enqueue(conn, "audit", dict(item))

    def failed(op: str, error: BaseException) -> None:
        _line(context, f"runner.ci request_failed op={op} error={type(error).__name__}")

    area = Area(
        workvms, enqueue_audit, lambda text: _line(context, text), browsers=browsers_of(context, workvms), env=env
    )
    socket, gid = found
    server = await serve(socket, area.handle, gid=gid, failed=failed)
    _line(context, "runner.ci listening")
    try:
        await context.stopping.wait()
    finally:
        server.close()
        await area.close()


TASKS = (TaskSpec("ci", run_ci),)
