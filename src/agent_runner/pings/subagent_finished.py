"""`subagent.finished` ping (RUN-11 extension point `<area>/subagent_finished.py:NOTIFY`, 05-runner.md section 13):
the parent session learns content-free that a turn of a child session, or the child, ended (`ref` = child session ID;
ADR-088 section 1); the result is read with `get_subagent_result`. Sent through `send.send_ping`."""

from __future__ import annotations

from typing import Any

from agent_runner.pings.send import send_ping
from agent_runner.subagents.extensions import FinishedPing


async def notify(context: Any, ping: FinishedPing) -> None:
    await send_ping(ping.parent_session_id, ping.kind, None, ping.child_session_id, ping.ping_id, context=context)


NOTIFY = notify
