"""U-RUN-queries read log: every call of a read tool becomes one outbox audit `read.session.<tool>` and - when the tool
checked its arguments - one `platform.read` event `{tool, arguments, result_count}` of the session's workgroup
(03-api.md sections 6.1, 8 and 10, 02-data-model.md section 2.9), also for reads refused on rights; arguments a tool
refused are audited but never sealed (KOR-03, AUD-01 A-18); event bodies and audits match `contracts/`."""

from __future__ import annotations

from typing import Any

from ..reconcile.rig import Rig
from ..sync.fakehub import SCHEMAS
from .kit import BOB, ORG, RDEV, TASK, TASK_ONE, WG, WG_ONE, RKit, run, task_row

CALLS: tuple[tuple[str, dict[str, Any], str, int], ...] = (
    ("get_context", {}, "ok", 1),
    ("list_workgroups", {}, "ok", 3),
    ("list_members", {"scope": "org"}, "ok", 5),
    ("list_members", {"scope": "workgroup"}, "ok", 4),
    ("list_tasks", {"limit": 5}, "ok", 1),
    ("get_task", {"task_id": TASK}, "ok", 1),
    ("get_task_messages", {"task_id": TASK}, "ok", 1),
    ("get_whiteboard", {"level": "org"}, "ok", 0),
    ("list_operations", {}, "ok", 0),
    ("get_task", {"task_id": TASK_ONE}, "denied", 0),
    ("get_operation", {"operation_id": "01930007-0000-7000-8000-000000000001"}, "denied", 0),
    ("list_members", {"scope": "nobody"}, "failed", 0),
)
TARGETS = {
    "get_context": "session",
    "list_workgroups": "org",
    "list_tasks": "workgroup",
    "get_task": "task",
    "get_task_messages": "task",
    "get_whiteboard": "org",
    "list_operations": "task",
}


def test_every_call_is_one_read_event_and_one_audit(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        k.events.prompt(TASK, "Hello")
        await k.pull()
        k.hub.tasks = [task_row(TASK), task_row(TASK_ONE, WG_ONE)]
        sid = await k.session(1)
        for tool, arguments, _result, _count in CALLS:
            await k.read(sid, tool, arguments)
        events = await k.reads()
        sealed = [call for call in CALLS if call[2] != "failed"]
        assert [(b["tool"], b["arguments"], b["result_count"]) for _h, b in events] == [
            (tool, arguments, count) for tool, arguments, _result, count in sealed
        ]
        for header, read in events:
            assert (header["scope"], header["scope_id"], header["org"]) == ("wg", WG, ORG)
            assert (header["task"], header["session"], header["author"]) == (TASK, sid, RDEV)
            assert SCHEMAS.errors("events/platform.read.json", read) == []
        audits = await k.read_audits()
        assert [(a["action"], a["result"], a["detail"]) for a in audits] == [
            (f"read.session.{tool}", result, {"result_count": count}) for tool, _a, result, count in CALLS
        ]
        # every event names its read target in `ref` (the audited target; the hub refuses an empty ref)
        assert [header["ref"] for header, _b in events] == [
            audit["target_id"] for audit, call in zip(audits, CALLS, strict=True) if call[2] != "failed"
        ]
        for audit, (tool, _arguments, result, _count) in zip(audits, CALLS, strict=True):
            assert (audit["task_id"], audit["session_id"]) == (TASK, sid)
            assert SCHEMAS.errors(f"audit/read.session.{tool}.json", audit["detail"]) == []
            if result == "ok" and tool in TARGETS:
                assert audit["target_kind"] == TARGETS[tool], tool
        # the outbox (events and audits) passes the hub's contract check of `POST /v1/device/outbox`
        assert k.env.hub.items()
        task_audit = next(a for a in audits if a["action"] == "read.session.get_task")
        assert (task_audit["target_kind"], task_audit["target_id"]) == ("task", TASK)

    run(rig, body)


def test_reads_of_a_session_with_lost_rights_are_audited_as_denied(rig: Rig) -> None:
    async def body(k: RKit) -> None:
        sid = await k.session(1, wg=WG_ONE, owner=BOB, task=TASK_ONE)
        await k.extend(lambda log: log.add("ungrant", "alice", subject=BOB, wg=WG_ONE))
        assert await k.error(sid, "list_tasks") == "forbidden"
        audits = await k.read_audits()
        assert [(a["action"], a["result"]) for a in audits] == [("read.session.list_tasks", "denied")]
        events = await k.reads()
        assert [(h["scope_id"], b["result_count"]) for h, b in events] == [(WG_ONE, 0)]

    run(rig, body)
