"""U-RUN-queries read log contract (KOR-03, AUD-01 A-18): a `platform.read` event is sealed only with arguments the
tool checked, so every sealed body matches `events/platform.read.json` (whose `arguments` are the tool's input
schema). A call whose arguments a tool refuses (`params_invalid`) is still audited (`failed`), but has no event."""

from __future__ import annotations

from typing import Any

from ..reconcile.rig import Rig
from ..sync.fakehub import SCHEMAS
from .kit import TASK, RKit, run, task_row

UUID = "01930007-0000-7000-8000-000000000001"
# get_whiteboard: task_id exactly for level task, a rule the tool schema cannot state (contracts/mcp/objects.json)
RULE_ONLY: tuple[dict[str, Any], ...] = ({"level": "org", "task_id": TASK}, {"level": "task"})
# Per tool: arguments its input schema refuses (extra key, missing required, wrong type, out of range, bad format).
INVALID: tuple[tuple[str, dict[str, Any]], ...] = (
    ("get_context", {"x": 1}),
    ("list_workgroups", {"scope": "org"}),
    ("list_members", {}),
    ("list_members", {"scope": 5}),
    ("list_members", {"scope": "org", "x": 1}),
    ("list_tasks", {"limit": 0}),
    ("list_tasks", {"limit": True}),
    ("list_tasks", {"state": "nope"}),
    ("list_tasks", {"before": "a b"}),
    ("get_task", {}),
    ("get_task", {"task_id": "not-a-uuid"}),
    ("get_task_messages", {"task_id": TASK, "after": -1}),
    ("get_task_messages", {"task_id": TASK, "limit": 201}),
    ("get_whiteboard", RULE_ONLY[0]),
    ("get_whiteboard", RULE_ONLY[1]),
    ("get_whiteboard", {"level": "everything"}),
    ("list_operations", {"limit": 101}),
    ("get_operation", {"operation_id": "x"}),
    ("get_operation", {"operation_id": UUID, "wait_seconds": 31}),
)
VALID: tuple[tuple[str, dict[str, Any]], ...] = (
    ("get_context", {}),
    ("list_tasks", {"limit": 2, "state": "working"}),
    ("get_task", {"task_id": TASK}),
    ("get_whiteboard", {"level": "task", "task_id": TASK}),
    ("get_operation", {"operation_id": UUID, "wait_seconds": 0}),
)


def test_refused_arguments_are_audited_but_never_sealed(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.hub.tasks = [task_row(TASK)]
        sid = await k.session(1)
        for tool, arguments in INVALID:
            assert await k.error(sid, tool, arguments) == "params_invalid", (tool, arguments)
            body = {"tool": tool, "arguments": arguments, "result_count": 0}
            # the input schema refuses them too, except the whiteboard rule
            assert bool(SCHEMAS.errors("events/platform.read.json", body)) != (arguments in RULE_ONLY), body
        for tool, arguments in VALID:
            await k.read(sid, tool, arguments)
        events = await k.reads()
        assert [(b["tool"], b["arguments"]) for _h, b in events] == [(t, a) for t, a in VALID]
        for _header, read in events:
            assert SCHEMAS.errors("events/platform.read.json", read) == [], read
        audits = await k.read_audits()
        assert [(a["action"], a["result"]) for a in audits[: len(INVALID)]] == [
            (f"read.session.{tool}", "failed") for tool, _a in INVALID
        ]
        assert len(audits) == len(INVALID) + len(VALID)
        assert any(
            line.startswith("runner.subagents query_arguments_refused tool=get_context") for line in k.skit.lines
        )

    run(rig, body)
