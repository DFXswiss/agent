"""Read tools of this area at the discovered point `<area>/query_tools.py:TOOLS` (ADR-038; 03-api.md section 8.1,
ADR-024): the forwarded MCP reads of the guest (`platform.query`, GST-07) except `get_subagent_result` (RUN-11) and
`get_notifications` (answered in the guest, RUN-14). The dispatcher records every outcome as `platform.read` event
`{tool, arguments, result_count}` and `read.session.<tool>` audit and answers the pending call.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.queries.tools import (
    get_context,
    get_operation,
    get_task,
    get_task_messages,
    get_whiteboard,
    list_members,
    list_operations,
    list_tasks,
    list_workgroups,
)

TOOLS: dict[str, Callable[..., Any]] = {
    module.__name__.rsplit(".", 1)[-1]: module.TOOL
    for module in (
        get_context,
        list_workgroups,
        list_members,
        list_tasks,
        get_task,
        get_task_messages,
        get_whiteboard,
        list_operations,
        get_operation,
    )
}
