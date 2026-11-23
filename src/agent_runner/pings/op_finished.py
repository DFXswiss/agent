"""`operation.finished` ping (RUN-07 extension point `<area>/op_finished.py:NOTIFY`, 05-runner.md section 12 step 7):
an operation became final after its MCP call had already returned `pending`; the session learns it content-free
(`ref` = operation ID) and reads the result with `get_operation`. The ping ID is stable per operation
(`send.stable_ping_id`), so a repeated notification is the same ping. Sent through `send.send_ping`."""

from __future__ import annotations

from typing import Any

from agent_runner.operations.records import Operation
from agent_runner.pings.send import send_ping, stable_ping_id

KIND = "operation.finished"


async def notify(context: Any, operation: Operation) -> None:
    await send_ping(operation.session_id, KIND, None, operation.id, stable_ping_id(KIND, operation.id), context=context)


NOTIFY = notify
