"""Content-free pings in the guest (RUN-14; 05-runner.md section 9, 03-api.md sections 7.1, 8.1 and 8.2, ADR-020).

The control process keeps the pings (``POST /v1/sessions/{id}/pings``); the hook addon attaches the fixed notice once
per pending batch to PreToolUse, PostToolUse and accepted UserPromptSubmit answers (never to Stop); the tool
``get_notifications`` lists and marks them; every delivery is journaled as ``ping.delivered``. Nothing is typed into
the TUI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_guest import delivery, tmux
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.control import ControlContext
from agent_guest.hook_addons import pings as addon
from agent_guest.hooks import HookCall
from agent_guest.mcp import ToolCall, ToolError, load_tools
from agent_guest.pings import KINDS, PingStore, notice_text
from agent_guest.schema import SchemaSet
from agent_guest.state import Session, StateError
from tests.platform.guest.conftest import ControlClient, FakeClock, make_session
from tests.platform.guest.hooks.conftest import OTHER_SESSION_ID, OTHER_TOKEN, HookClient, bodies, hook_body, tool_body
from tests.platform.guest.mcp.conftest import McpClient

ENTRY = "01a1ee56-5e00-7003-8f1b-340691d99e01"
CHILD = "01a1ee56-5e00-7003-8f1b-340691d99e02"
OPERATION = "01a1ee56-5e00-7003-8f1b-340691d99e03"
PING_A = "01a1ee56-5e00-7003-8f1b-340691d99a01"
PING_B = "01a1ee56-5e00-7003-8f1b-340691d99a02"
PING_C = "01a1ee56-5e00-7003-8f1b-340691d99a03"
WHITEBOARD = {"ping_id": PING_A, "kind": "whiteboard.entry", "level": "task", "ref": ENTRY}
SUBAGENT = {"ping_id": PING_B, "kind": "subagent.finished", "ref": CHILD}
OPERATION_DONE = {"ping_id": PING_C, "kind": "operation.finished", "ref": OPERATION}
PROMPT_ID = "01a1ee56-5e00-7003-8f1b-340691d99b01"


@pytest.fixture
def hooks_(api_port: int) -> HookClient:
    return HookClient(api_port)


@pytest.fixture
def mcp(api_port: int) -> McpClient:
    return McpClient(api_port)


def send(context: ControlContext, ping: dict[str, Any], session_id: str | None = None) -> tuple[int, Any]:
    target = session_id or ping_session(context)
    reply = ControlClient(context.config.control_socket).request("POST", f"/v1/sessions/{target}/pings", ping)
    return reply.status, reply.json()


def ping_session(context: ControlContext) -> str:
    sessions = context.state.sessions()
    assert sessions
    return sessions[0].id


def notifications(mcp: McpClient) -> Any:
    result = mcp.tool("get_notifications", {})
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def context_of(answer: dict[str, Any]) -> str | None:
    output = answer.get("hookSpecificOutput", {})
    found = output.get("additionalContext")
    return found if isinstance(found, str) else None


def register(context: ControlContext, session_id: str, text: str) -> None:
    reply = ControlClient(context.config.control_socket).request(
        "POST", f"/v1/sessions/{session_id}/prompts", {"prompt_id": PROMPT_ID, "text": text}
    )
    assert reply.status == 200


# ---------------------------------------------------------------------------------------------- notice text


def test_notice_text_is_the_contract_text(contracts_dir: Path, schemas: SchemaSet) -> None:
    fixture = contracts_dir / "fixtures" / "mcp" / "hooks" / "PostToolUse.response" / "valid" / "notice.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert notice_text(2, ["operation.finished", "whiteboard.entry", "operation.finished"]) == expected
    for count, kinds in ((1, ["subagent.finished"]), (3, list(reversed(KINDS))), (999999, list(KINDS))):
        text = notice_text(count, kinds)
        assert schemas.errors("mcp/objects.json#/$defs/notice", text) == []
    assert notice_text(3, list(reversed(KINDS))).endswith(
        "(whiteboard.entry, subagent.finished, operation.finished). Use mcp__platform__get_notifications to see them."
    )


# ---------------------------------------------------------------------------------------------- control route


def test_a_ping_is_stored_idempotently_without_touching_the_tui(
    context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch, hooks_: HookClient, mcp: McpClient
) -> None:
    spied: list[str] = []

    def spy(name: str) -> Any:
        def call(*_args: object, **_kwargs: object) -> Any:
            spied.append(name)
            raise AssertionError(name)

        return call

    monkeypatch.setattr(subprocess, "run", spy("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", spy("subprocess.Popen"))
    monkeypatch.setattr(delivery, "manager", spy("delivery.manager"))
    monkeypatch.setattr(tmux.Tmux, "send_keys", spy("tmux.send_keys"), raising=False)
    for ping in (WHITEBOARD, SUBAGENT, WHITEBOARD):
        assert send(context, ping) == (200, {})
    rows = context.journal._db.execute("SELECT id, session_id, kind, level, ref FROM ping ORDER BY id").fetchall()
    assert rows == [
        (PING_A, session.id, "whiteboard.entry", "task", ENTRY),
        (PING_B, session.id, "subagent.finished", None, CHILD),
    ]
    assert bodies(context, "ping.delivered") == []
    # the hook notice and the tool do not reach the TUI either
    notice = hooks_.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json()
    assert context_of(notice) == notice_text(2, ["whiteboard.entry", "subagent.finished"])
    assert [item["ping_id"] for item in notifications(mcp)["notifications"]] == [PING_A, PING_B]
    assert spied == []


def test_ping_requests_are_checked(context: ControlContext, session: Session) -> None:
    assert send(context, {**WHITEBOARD, "ping_id": "x"})[0] == 400
    assert send(context, {**SUBAGENT, "level": "task"})[0] == 400
    assert send(context, {**WHITEBOARD, "text": "content"})[0] == 400
    assert send(context, WHITEBOARD, OTHER_SESSION_ID) == (404, {"error": "not_found"})
    context.state.save_session(make_session(OTHER_SESSION_ID, OTHER_TOKEN, state="stopped"))
    assert send(context, WHITEBOARD, OTHER_SESSION_ID) == (409, {"error": "state_conflict"})
    assert send(context, WHITEBOARD) == (200, {})
    assert send(context, {**WHITEBOARD, "ref": OPERATION}) == (409, {"error": "id_conflict"})
    context.state.save_session(make_session(OTHER_SESSION_ID, OTHER_TOKEN))
    assert send(context, WHITEBOARD, OTHER_SESSION_ID) == (409, {"error": "id_conflict"})


def test_ping_ops_check_their_arguments(context: ControlContext, api_client: ApiClient, session: Session) -> None:
    for op in ("ping.notice", "ping.take"):
        for args in ({}, {"session_id": 5}, {"session_id": session.id, "x": 1}):
            with pytest.raises(ApiSockError) as caught:
                api_client.call(op, args)
            assert caught.value.code == "request_invalid"
        with pytest.raises(ApiSockError) as caught:
            api_client.call(op, {"session_id": OTHER_SESSION_ID})
        assert caught.value.code == "not_found"
    assert api_client.call("ping.notice", {"session_id": session.id}) == {"notice": None}
    assert api_client.call("ping.take", {"session_id": session.id}) == {"notifications": []}


# ---------------------------------------------------------------------------------------------- hook notice


@pytest.mark.parametrize(
    ("event", "body"),
    [
        ("PreToolUse", tool_body("PreToolUse", "Grep", {"pattern": "x"})),
        ("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})),
    ],
)
def test_the_notice_is_attached_once_per_batch_to_tool_answers(
    *,
    context: ControlContext,
    session: Session,
    hooks_: HookClient,
    schemas: SchemaSet,
    clock: FakeClock,
    event: str,
    body: Any,
) -> None:
    assert context_of(hooks_.post(event, body).json()) is None
    send(context, OPERATION_DONE)
    clock.advance(1)
    send(context, WHITEBOARD)
    first = hooks_.post(event, body).json()
    assert context_of(first) == notice_text(2, ["whiteboard.entry", "operation.finished"])
    assert first["hookSpecificOutput"]["hookEventName"] == event
    assert schemas.errors(f"mcp/hooks/{event}.response.json", first) == []
    assert context_of(hooks_.post(event, body).json()) is None
    send(context, SUBAGENT)
    assert context_of(hooks_.post(event, body).json()) == notice_text(1, ["subagent.finished"])
    assert bodies(context, "ping.delivered") == [
        {"ping_id": PING_C, "via": "hook"},
        {"ping_id": PING_A, "via": "hook"},
        {"ping_id": PING_B, "via": "hook"},
    ]


def test_a_denied_tool_still_carries_the_notice(context: ControlContext, session: Session, hooks_: HookClient) -> None:
    send(context, WHITEBOARD)
    answer = hooks_.post("PreToolUse", tool_body("PreToolUse", "Bash", {"command": "ls"})).json()
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert context_of(answer) == notice_text(1, ["whiteboard.entry"])


def test_stop_and_other_events_never_carry_the_notice(
    context: ControlContext, session: Session, hooks_: HookClient
) -> None:
    send(context, WHITEBOARD)
    assert hooks_.post("Stop", hook_body("Stop", stop_hook_active=False)).json() == {}
    assert hooks_.post("StopFailure", hook_body("StopFailure", error="x")).json() == {}
    assert hooks_.post("Notification", hook_body("Notification", message="m")).json() == {}
    assert bodies(context, "ping.delivered") == []
    assert notifications_rows(context) == [(PING_A, None, None)]


def notifications_rows(context: ControlContext) -> list[tuple[str, str | None, str | None]]:
    rows = context.journal._db.execute("SELECT id, noticed_at, delivered_at FROM ping ORDER BY id").fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def test_an_idle_session_sees_the_ping_with_its_next_prompt(
    context: ControlContext, session: Session, hooks_: HookClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert hooks_.post("Stop", hook_body("Stop", stop_hook_active=False)).json() == {}
    idle = context.state.session(session.id)
    assert idle is not None
    assert idle.state == "idle"
    send(context, WHITEBOARD)
    # Nothing happens while the session is idle: no hook, no delivery.
    assert notifications_rows(context) == [(PING_A, None, None)]
    assert bodies(context, "ping.delivered") == []
    # A blocked prompt consumes nothing; the next platform prompt shows the notice.
    typed = hooks_.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="typed")).json()
    assert typed["decision"] == "block"
    assert context_of(typed) is None
    monkeypatch.setattr(delivery.Delivery, "submit", lambda *_args: None)
    register(context, session.id, "next prompt")
    answer = hooks_.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="next prompt")).json()
    assert answer == {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": notice_text(1, ["whiteboard.entry"]),
        }
    }
    assert bodies(context, "ping.delivered") == [{"ping_id": PING_A, "via": "hook"}]


def test_a_failing_notice_op_leaves_the_decision(
    context: ControlContext, session: Session, hooks_: HookClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    send(context, WHITEBOARD)

    def broken(_context: object, _args: object) -> dict[str, Any]:
        raise StateError("unavailable")

    monkeypatch.setattr("agent_guest.control_routes.pings.PingStore.notice", broken)
    answer = hooks_.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json()
    assert answer == {}
    assert bodies(context, "guest.error") == [{"code": "hook_handler_failed"}]


def test_the_addon_handles_only_decorated_calls() -> None:
    assert addon.ADDON.events == frozenset({"PreToolUse", "PostToolUse", "UserPromptSubmit"})
    assert "Stop" not in addon.ADDON.events

    class Unused:
        def call(self, *_args: object) -> None:
            raise AssertionError("no op for an undecorated call")

    call = HookCall(event="UserPromptSubmit", session_id="s", body=None, decision="block", decorate=False)
    assert addon.handle(SimpleNamespace(control=Unused()), call) is None  # type: ignore[arg-type]

    class Odd:
        def call(self, *_args: object) -> dict[str, Any]:
            return {"notice": 5}

    allowed = HookCall(event="PreToolUse", session_id="s", body=None, decision="allow", decorate=True)
    assert addon.handle(SimpleNamespace(control=Odd()), allowed) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------- get_notifications


def test_get_notifications_lists_and_marks_delivered(
    *,
    context: ControlContext,
    session: Session,
    mcp: McpClient,
    hooks_: HookClient,
    clock: FakeClock,
    schemas: SchemaSet,
) -> None:
    assert notifications(mcp) == {"notifications": []}
    send(context, WHITEBOARD)
    clock.advance(1)
    send(context, SUBAGENT)
    # The hook noticed the batch; the tool still lists it once.
    assert context_of(hooks_.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json())
    clock.advance(1)
    send(context, OPERATION_DONE)
    listed = notifications(mcp)
    assert schemas.errors("mcp/tools/get_notifications.output.json", listed) == []
    stored = dict(context.journal._db.execute("SELECT id, created_at FROM ping").fetchall())
    assert listed == {
        "notifications": [
            {**WHITEBOARD, "at": stored[PING_A]},
            {**SUBAGENT, "at": stored[PING_B]},
            {**OPERATION_DONE, "at": stored[PING_C]},
        ]
    }
    assert notifications(mcp) == {"notifications": []}
    assert context_of(hooks_.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json()) is None
    assert bodies(context, "ping.delivered") == [
        {"ping_id": PING_A, "via": "hook"},
        {"ping_id": PING_B, "via": "hook"},
        {"ping_id": PING_A, "via": "tool"},
        {"ping_id": PING_B, "via": "tool"},
        {"ping_id": PING_C, "via": "tool"},
    ]
    # Every call of the tool is a read and journaled as such, also an empty one (03-api.md section 8, KOR-07).
    assert bodies(context, "platform.read") == [
        {"tool": "get_notifications", "result_count": count} for count in (0, 3, 0)
    ]
    # A repeated ping that was delivered journals its delivery again (a runner that lost it learns it once more).
    assert send(context, SUBAGENT) == (200, {})
    assert bodies(context, "ping.delivered")[-1] == {"ping_id": PING_B, "via": "tool"}


def test_get_notifications_is_per_session(context: ControlContext, session: Session, mcp: McpClient) -> None:
    context.state.save_session(make_session(OTHER_SESSION_ID, OTHER_TOKEN))
    send(context, WHITEBOARD, OTHER_SESSION_ID)
    assert notifications(mcp) == {"notifications": []}
    other = McpClient(mcp.port)
    result = json.loads(
        other.post(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_notifications", "arguments": {}},
                }
            ).encode(),
            token=OTHER_TOKEN,
        ).json()["result"]["content"][0]["text"]
    )
    assert [item["ping_id"] for item in result["notifications"]] == [PING_A]


def test_a_noticed_ping_sent_again_journals_its_hook_delivery(
    context: ControlContext, session: Session, hooks_: HookClient
) -> None:
    send(context, WHITEBOARD)
    hooks_.post("PreToolUse", tool_body("PreToolUse", "Grep", {"pattern": "x"}))
    send(context, WHITEBOARD)
    assert bodies(context, "ping.delivered") == [{"ping_id": PING_A, "via": "hook"}] * 2


def test_get_notifications_lists_at_most_the_limit(
    context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_guest.pings.MAX_LIST", 1)
    store = PingStore(context.journal, context.state)
    store.store(session.id, WHITEBOARD)
    store.store(session.id, SUBAGENT)
    assert [item["ping_id"] for item in store.take(session.id)] == [PING_A]
    assert [item["ping_id"] for item in store.take(session.id)] == [PING_B]


def test_get_notifications_without_the_control_process_is_unavailable() -> None:
    tool = load_tools()["get_notifications"]

    class Down:
        def call(self, *_args: object) -> dict[str, Any]:
            raise ApiSockError("unavailable")

    with pytest.raises(ToolError) as caught:
        tool.handler(ToolCall("get_notifications", "s", {}, Down(), None))  # type: ignore[arg-type]
    assert caught.value.code == "unavailable"
