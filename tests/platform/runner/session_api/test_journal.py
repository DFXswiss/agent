"""Journal `platform.read` and `platform.refused` -> `platform.read` event and read audits (KOR-07; 03-api.md sections
7.2, 8 and 10: every MCP call is journaled, every read is an audit row)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner import registry
from agent_runner.session_api import journal
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.subagents.journal import READ_TOOLS
from agent_runner.sync.outbox import open_item

from ..pings.kit import TASK, TASK_OTHER, WG, WG_OTHER, PKit, run
from ..reconcile.rig import Rig

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"


async def handle(p: PKit, records: list[tuple[str | None, str, dict[str, Any]]], wg_id: str = WG) -> VmJournal:
    reader = VmJournal(p.kit.session_env, await p.vm(wg_id), handlers=journal.HANDLERS)
    for sid, kind, body in records:
        p.guest.emit(sid, kind, body)
    await reader.handle(p.guest.records[-len(records) :])
    return reader


async def items(p: PKit, kind: str) -> list[dict[str, Any]]:
    rows = await p.env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
    key = p.env.sync.storage_key
    return [open_item(key, seq, item_kind, bytes(blob)) for seq, item_kind, blob in rows if item_kind == kind]


def test_the_area_is_registered() -> None:
    handlers = registry.journal_handlers()
    assert handlers["platform.read"] is journal.platform_read
    assert handlers["platform.refused"] is journal.platform_refused
    assert journal.GUEST_READS < READ_TOOLS
    assert {"request_operation"} | READ_TOOLS == journal.TOOLS


def test_a_read_answered_by_the_guest_is_recorded_and_audited(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        await handle(p, [(sid, "platform.read", {"tool": "get_notifications", "result_count": 2})])
        assert await p.env.audits() == [
            {
                "action": "read.session.get_notifications",
                "result": "ok",
                "task_id": TASK,
                "session_id": sid,
                "target_kind": "session",
                "target_id": sid,
                "detail": {"result_count": 2},
            }
        ]
        [event] = await items(p, "event")
        assert event["header"]["kind"] == "platform.read"
        assert event["header"]["session"] == sid
        assert event["header"]["ref"] == sid
        assert event["body"] == {"tool": "get_notifications", "arguments": {}, "result_count": 2}
        # the contract-checking hub double accepts both items
        await p.env.sync.pusher.drain()
        kinds = [(item["item_kind"], item["body"].get("action")) for item in p.env.hub.items()]
        assert ("audit", "read.session.get_notifications") in kinds
        assert any(kind == "event" for kind, _ in kinds)

    run(rig, body)


def test_refused_calls_audit_only_read_tools(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        sid = await p.session(1)
        await handle(
            p,
            [
                (sid, "platform.refused", {"code": "params_invalid", "tool": "get_task"}),
                (sid, "platform.refused", {"code": "params_invalid", "tool": "request_operation"}),
                (sid, "platform.refused", {"code": "params_invalid", "tool": None}),
                (sid, "platform.refused", {"code": "method_not_found", "tool": None}),
                (sid, "platform.refused", {"code": "method_not_found", "tool": None, "method": "server/discover"}),
                # KOR-10 (AUD-02 NB-07, NB-08)
                (sid, "platform.refused", {"code": "rate_limited", "tool": "list_tasks"}),
                (sid, "platform.refused", {"code": "not_found", "tool": "get_subagent_result"}),
                (sid, "platform.refused", {"code": "size_exceeded", "tool": None}),
            ],
        )
        assert await p.env.audits() == [
            {
                "action": f"read.session.{tool}",
                "result": "rejected",
                "task_id": TASK,
                "session_id": sid,
                "target_kind": "session",
                "target_id": sid,
                "detail": {"result_count": 0},
            }
            for tool in ("get_task", "list_tasks", "get_subagent_result")
        ]
        assert await items(p, "event") == []
        line = f"runner.session_api refused session_id={sid} code="
        assert f"{line}params_invalid tool=get_task method=-" in p.lines
        assert f"{line}params_invalid tool=request_operation method=-" in p.lines
        assert f"{line}method_not_found tool=- method=-" in p.lines  # a guest before HOT-09
        assert f"{line}method_not_found tool=- method=server/discover" in p.lines
        assert f"{line}size_exceeded tool=- method=-" in p.lines
        await p.env.sync.pusher.drain()
        assert [item["body"]["action"] for item in p.env.hub.items() if item["item_kind"] == "audit"] == [
            "read.session.get_task",
            "read.session.list_tasks",
            "read.session.get_subagent_result",
        ]

    run(rig, body)


def test_records_of_a_session_on_another_vm_change_nothing(rig: Rig) -> None:
    async def body(p: PKit) -> None:
        await p.session(1)
        other = await p.session(2, wg_id=WG_OTHER, task_id=TASK_OTHER)
        await handle(
            p,
            [
                (other, "platform.read", {"tool": "get_notifications", "result_count": 1}),
                (other, "platform.refused", {"code": "params_invalid", "tool": "get_task"}),
            ],
        )
        assert await p.env.audits() == []
        assert sum("foreign_session" in line for line in p.lines) == 2

    run(rig, body)


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("platform.read", {"tool": "get_task", "result_count": 1}),
        ("platform.read", {"tool": "get_notifications", "result_count": -1}),
        ("platform.read", {"tool": "get_notifications", "result_count": True}),
        ("platform.read", {"tool": "get_notifications", "result_count": 2**53}),
        ("platform.read", {"tool": "get_notifications", "result_count": 1, "arguments": {}}),
        ("platform.refused", {"code": "unknown_tool", "tool": "get_task"}),
        ("platform.refused", {"code": "params_invalid", "tool": "read_secrets"}),
        ("platform.refused", {"code": "-32602", "tool": None}),
        ("platform.refused", {"code": "params_invalid"}),
        ("platform.refused", {"code": "rate_limited", "tool": "request_operation"}),
        ("platform.refused", {"code": "rate_limited", "tool": None}),
        ("platform.refused", {"code": "not_found", "tool": "get_task"}),
        ("platform.refused", {"code": "size_exceeded", "tool": "get_task"}),
        ("platform.refused", {"code": "method_not_found", "tool": None, "method": "tools/call\nforged"}),
        ("platform.refused", {"code": "params_invalid", "tool": None, "method": "server/discover"}),
        ("platform.refused", {"code": "method_not_found", "tool": "get_task", "method": "other"}),
    ],
)
def test_malformed_records_are_skipped(rig: Rig, kind: str, body: dict[str, Any]) -> None:
    async def run_body(p: PKit) -> None:
        sid = await p.session(1)
        reader = await handle(p, [(sid, kind, body)])
        assert reader.skipped == 1
        assert await p.env.audits() == []
        assert await items(p, "event") == []

    run(rig, run_body)


@pytest.mark.parametrize("kind", ["platform.read", "platform.refused"])
def test_the_parsers_accept_exactly_the_contract_fixtures(kind: str) -> None:
    parse = journal.parse_read if kind == "platform.read" else journal.parse_refused
    folder = CONTRACTS / "fixtures" / "guest" / "journal" / kind
    for path in sorted(folder.glob("valid/*.json")):
        parse(json.loads(path.read_text(encoding="utf-8")))
    invalid = sorted(folder.glob("invalid/*.json"))
    assert invalid
    for path in invalid:
        with pytest.raises(JournalRecordInvalid):
            parse(json.loads(path.read_text(encoding="utf-8")))


def test_the_method_names_are_those_of_the_contract() -> None:
    record = json.loads((CONTRACTS / "guest" / "journal" / "record.json").read_text(encoding="utf-8"))
    [branch] = [item for item in record["$defs"]["body_platform_refused"]["oneOf"] if "method" in item["properties"]]
    assert set(branch["properties"]["method"]["enum"]) == journal.METHODS
