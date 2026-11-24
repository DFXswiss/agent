"""U-GST-hooks: ``POST /hooks/<Event>`` of the API process (05-runner.md sections 5.2-5.5, 03-api.md section 8.2).

Runs the real API server against the real control process (journal in tmp): authentication (401 paths), all 18
events with the contract fixtures, fail-closed answers for unreadable bodies, the PreToolUse policy and the prompt
origin end to end, PostToolUse file hints, the addon chain, the fault switch and the control-side ops.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_guest import hooks, registry, sessions
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.config import ConfigError, GuestConfig, load_config
from agent_guest.control import ControlContext
from agent_guest.control_routes import hooks as control_hooks
from agent_guest.hooks import HookAddon, HookCall
from agent_guest.journal import JournalError
from agent_guest.policy import BLOCK_REASON, REASONS, TOOL_FORBIDDEN
from agent_guest.routing import BodyReader, HttpError, Request, Response, Router
from agent_guest.sandbox import settings
from agent_guest.schema import SchemaSet
from agent_guest.server import ApiContext, ApiHttpServer, create_server
from agent_guest.state import Session, StateError
from tests.platform.guest.conftest import SESSION_TOKEN, ControlClient, make_session
from tests.platform.guest.hooks.conftest import (
    OTHER_SESSION_ID,
    OTHER_TOKEN,
    HookClient,
    bodies,
    hook_body,
    records,
    tool_body,
)

PROMPT_ID = "01a1ee56-6000-7003-8f1b-340691d99f10"
NOTICE = (
    "agent.dfx.swiss notice: 1 new item(s) available (whiteboard.entry). "
    "Use mcp__platform__get_notifications to see them."
)
EVENT_FIELDS: dict[str, dict[str, Any]] = {
    "UserPromptSubmit": {"prompt": "hello"},
    "PreToolUse": {"tool_name": "Read", "tool_input": {"file_path": "notes.txt"}, "tool_use_id": "toolu_01"},
    "PostToolUse": {"tool_name": "Read", "tool_input": {"file_path": "notes.txt"}, "tool_response": "n"},
    "PostToolUseFailure": {"tool_name": "Read", "tool_input": {"file_path": "x"}, "error": "missing"},
    "PermissionRequest": {"tool_name": "Bash", "tool_input": {"command": "id"}},
    "PermissionDenied": {"tool_name": "Bash", "tool_input": {"command": "id"}},
    "Notification": {"message": "waiting"},
}


def schema_of(event: str, kind: str) -> str:
    return f"mcp/hooks/{event}.{kind}.json"


def valid_body(schemas: SchemaSet, fixtures_dir: Path, event: str) -> dict[str, Any]:
    body = json.loads((fixtures_dir / f"{event}.request" / "valid" / "basic.json").read_text(encoding="utf-8"))
    body.update(EVENT_FIELDS.get(event, {}))
    assert schemas.errors(schema_of(event, "request"), body) == []
    return dict(body)


# ---------------------------------------------------------------- authentication


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic " + SESSION_TOKEN},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer " + OTHER_TOKEN},
    ],
    ids=["none", "basic", "bare", "empty", "unknown", "unknown-other"],
)
def test_hooks_need_the_session_token(hook_client: HookClient, session: Session, headers: dict[str, str]) -> None:
    reply = hook_client.post("PreToolUse", tool_body("PreToolUse", "Bash", {}), token=None, headers=headers)
    assert (reply.status, reply.json()) == (401, {"error": "unauthorized"})


def test_two_authorization_headers_are_refused(hook_client: HookClient, session: Session, api_port: int) -> None:
    connection = http.client.HTTPConnection("127.0.0.1", api_port, timeout=10)
    data = json.dumps(tool_body("PreToolUse", "Bash", {})).encode()
    connection.putrequest("POST", "/hooks/PreToolUse")
    for _ in range(2):
        connection.putheader("Authorization", f"Bearer {SESSION_TOKEN}")
    connection.putheader("Content-Length", str(len(data)))
    connection.endheaders(data)
    response = connection.getresponse()
    assert response.status == 401
    connection.close()


def test_token_of_a_stopped_session_is_refused(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    context.state.save_session(replace(session, state="stopped"))
    assert hook_client.post("Stop", hook_body("Stop")).status == 401


def test_lower_case_bearer_scheme_is_accepted(hook_client: HookClient, session: Session) -> None:
    reply = hook_client.post(
        "Stop", hook_body("Stop"), token=None, headers={"Authorization": f"bearer {SESSION_TOKEN}"}
    )
    assert (reply.status, reply.json()) == (200, {})


def test_unknown_event_is_not_found(hook_client: HookClient, session: Session) -> None:
    assert hook_client.post("Evil", hook_body("Evil")).status == 404


# ---------------------------------------------------------------- all 18 events


def test_events_are_the_contract_events(contracts_dir: Path) -> None:
    index = json.loads((contracts_dir / "mcp" / "INDEX.json").read_text(encoding="utf-8"))
    assert tuple(row["event"] for row in index["hooks"]) == hooks.EVENTS
    assert hooks.EVENTS == settings.HOOK_EVENTS
    assert [route.path for route in hooks.routes([])] == [f"/hooks/{event}" for event in hooks.EVENTS]
    registered = {route.path for route in registry.api_routes()}
    assert {f"/hooks/{event}" for event in hooks.EVENTS} <= registered


@pytest.mark.parametrize("event", hooks.EVENTS)
def test_every_event_answers_by_contract_and_is_journaled(
    hook_client: HookClient,
    context: ControlContext,
    *,
    ws_session: Session,
    schemas: SchemaSet,
    fixtures_dir: Path,
    event: str,
) -> None:
    if event == "UserPromptSubmit":
        register_prompt(context, ws_session.id, "hello")
    reply = hook_client.post(event, valid_body(schemas, fixtures_dir, event))
    assert reply.status == 200
    assert schemas.errors(schema_of(event, "response"), reply.json()) == []
    hooks_seen = bodies(context, "session.hook")
    assert [body["event"] for body in hooks_seen] == [event]
    expected = {"PreToolUse": "allow", "UserPromptSubmit": "allow"}.get(event, "none")
    assert hooks_seen[0]["decision"] == expected
    for body in hooks_seen:
        assert schemas.errors("guest/journal/session.hook.json", body) == []
    if event not in ("PreToolUse", "UserPromptSubmit"):
        assert reply.json() == {}


@pytest.mark.parametrize("event", hooks.EVENTS)
def test_every_valid_contract_fixture_is_answered(
    hook_client: HookClient, ws_session: Session, schemas: SchemaSet, fixtures_dir: Path, event: str
) -> None:
    for path in sorted((fixtures_dir / f"{event}.request" / "valid").glob("*.json")):
        reply = hook_client.post(event, json.loads(path.read_text(encoding="utf-8")))
        assert reply.status == 200, path.name
        assert schemas.errors(schema_of(event, "response"), reply.json()) == [], path.name


@pytest.mark.parametrize("event", hooks.EVENTS)
def test_invalid_bodies_fail_closed(
    hook_client: HookClient,
    context: ControlContext,
    *,
    session: Session,
    schemas: SchemaSet,
    fixtures_dir: Path,
    event: str,
) -> None:
    paths = sorted((fixtures_dir / f"{event}.request" / "invalid").glob("*.json"))
    assert paths
    for path in paths:
        reply = hook_client.post(event, json.loads(path.read_text(encoding="utf-8")))
        assert reply.status == 200, path.name
        assert reply.json() == closed_answer(event), path.name
        assert schemas.errors(schema_of(event, "response"), reply.json()) == []
    seen = bodies(context, "session.hook")
    assert len(seen) == len(paths)
    assert {body["decision"] for body in seen} == {
        {"PreToolUse": "deny", "UserPromptSubmit": "block"}.get(event, "none")
    }


def closed_answer(event: str) -> dict[str, Any]:
    return hooks.closed_answer(event) or {}


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[1]", b'{"a":1,"a":2}', b"\xff", b'{"x": NaN}'],
    ids=["text", "array", "duplicate-key", "not-utf8", "nan"],
)
def test_unreadable_bodies_fail_closed(hook_client: HookClient, session: Session, raw: bytes) -> None:
    assert hook_client.post("PreToolUse", raw=raw).json() == closed_answer("PreToolUse")
    assert hook_client.post("UserPromptSubmit", raw=raw).json() == closed_answer("UserPromptSubmit")
    assert hook_client.post("Stop", raw=raw).json() == {}


def test_oversized_body_fails_closed_without_reading_it(
    hook_client: HookClient, context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_MAX_BODY", 64)
    body = tool_body("PreToolUse", "Write", {"file_path": "a.txt", "content": "x" * 200})
    assert hook_client.post("PreToolUse", body).json() == closed_answer("PreToolUse")
    assert bodies(context, "session.hook") == [
        {"event": "PreToolUse", "decision": "deny", "rule": 4, "reason": REASONS["input_invalid"]}
    ]


def test_large_write_content_is_still_checked(
    hook_client: HookClient, context: ControlContext, ws_session: Session
) -> None:
    body = tool_body("PreToolUse", "Write", {"file_path": "big.txt", "content": "x" * (3 * 1024 * 1024)})
    answer = hook_client.post("PreToolUse", body).json()
    assert answer == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}


def test_oversized_policy_input_is_denied(
    hook_client: HookClient, context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "MAX_POLICY_INPUT", 32)
    body = tool_body("PreToolUse", "Read", {"file_path": "x" * 64})
    assert hook_client.post("PreToolUse", body).json() == closed_answer("PreToolUse")
    assert bodies(context, "session.hook")[0]["tool"] == "Read"


# ---------------------------------------------------------------- PreToolUse end to end


def test_pre_tool_use_denies_forbidden_tools(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    answer = hook_client.post("PreToolUse", tool_body("PreToolUse", "Bash", {"command": "id; git --version"})).json()
    reason = TOOL_FORBIDDEN.format(name="Bash")
    assert answer == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    assert bodies(context, "session.hook") == [
        {"event": "PreToolUse", "tool": "Bash", "decision": "deny", "rule": 2, "reason": reason}
    ]
    assert hooks.STATS.snapshot()["denies"] >= 1


def test_pre_tool_use_odd_tool_names_are_not_journaled(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    hook_client.post("PreToolUse", tool_body("PreToolUse", "Bash; id", {}))
    assert bodies(context, "session.hook") == [
        {"event": "PreToolUse", "decision": "deny", "rule": 2, "reason": REASONS["tool_forbidden_unnamed"]}
    ]


def test_pre_tool_use_resolves_symlinks_as_root(
    hook_client: HookClient, context: ControlContext, ws_session: Session, tmp_path: Path
) -> None:
    outside = tmp_path / "etc"
    outside.mkdir()
    (outside / "passwd").write_text("root", encoding="utf-8")
    os.symlink(str(outside), Path(ws_session.workspace) / "etc")
    allowed = hook_client.post("PreToolUse", tool_body("PreToolUse", "Read", {"file_path": "notes.txt"})).json()
    denied = hook_client.post("PreToolUse", tool_body("PreToolUse", "Read", {"file_path": "etc/passwd"})).json()
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == REASONS["read_outside_workspace"]
    first, second = bodies(context, "session.hook")
    assert first == {
        "event": "PreToolUse",
        "tool": "Read",
        "decision": "allow",
        "rule": 7,
        "path": f"{ws_session.workspace}/notes.txt",
    }
    assert second["path"] == str((outside / "passwd").resolve())
    assert (second["rule"], second["decision"]) == (6, "deny")


def test_pre_tool_use_allows_platform_tools(hook_client: HookClient, context: ControlContext, session: Session) -> None:
    body = tool_body("PreToolUse", "mcp__platform__request_operation", {"type": "x", "params": {}})
    answer = hook_client.post("PreToolUse", body).json()
    assert answer["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert bodies(context, "session.hook")[0]["rule"] == 1


def test_long_paths_are_not_journaled(hook_client: HookClient, context: ControlContext, ws_session: Session) -> None:
    hook_client.post("PreToolUse", tool_body("PreToolUse", "Read", {"file_path": "a/" * 2100 + "x"}))
    record = bodies(context, "session.hook")[0]
    assert record["decision"] == "allow"
    assert "path" not in record


# ---------------------------------------------------------------- UserPromptSubmit end to end


def register_prompt(context: ControlContext, session_id: str, text: str, prompt_id: str = PROMPT_ID) -> None:
    client = ControlClient(context.config.control_socket)
    reply = client.request("POST", f"/v1/sessions/{session_id}/prompts", {"prompt_id": prompt_id, "text": text})
    assert reply.status == 200


def test_user_prompt_submit_accepts_registered_prompts_once(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    register_prompt(context, session.id, "What is 2+2?")
    first = hook_client.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="What is 2+2?"))
    second = hook_client.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="What is 2+2?"))
    assert (first.json(), second.json()) == ({}, {"decision": "block", "reason": BLOCK_REASON})
    states = bodies(context, "session.state")
    assert [state["state"] for state in states] == ["prompt_accepted", "prompt_refused"]
    assert states[0]["prompt_id"] == PROMPT_ID


def test_user_prompt_submit_refuses_a_prompt_of_another_session(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    context.state.save_session(make_session(OTHER_SESSION_ID, OTHER_TOKEN))
    register_prompt(context, OTHER_SESSION_ID, "theirs")
    reply = hook_client.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="theirs"))
    assert reply.json() == {"decision": "block", "reason": BLOCK_REASON}
    other = HookClient(hook_client.port, OTHER_TOKEN).post(
        "UserPromptSubmit", hook_body("UserPromptSubmit", prompt="theirs")
    )
    assert other.json() == {}


# ---------------------------------------------------------------- other events


def test_subagent_start_raises_a_guest_error(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    assert hook_client.post("SubagentStart", hook_body("SubagentStart", agent_type="x")).json() == {}
    assert bodies(context, "session.hook") == [{"event": "SubagentStart", "decision": "none"}]
    assert bodies(context, "guest.error") == [{"code": "subagent_hook_seen"}]


def test_stop_is_journaled_and_the_idle_addon_ends_the_turn(
    hook_client: HookClient, context: ControlContext, session: Session
) -> None:
    # The answer stays {}; the state change comes from the addon hook_addons/idle.py (GST-06).
    assert hook_client.post("Stop", hook_body("Stop", stop_hook_active=False)).json() == {}
    assert [record["kind"] for record in records(context)] == ["session.hook", "session.state"]
    state = context.state.session(session.id)
    assert state is not None
    assert state.state == "idle"


def test_post_tool_use_of_write_records_the_tool_and_the_file_hint(
    hook_client: HookClient, context: ControlContext, ws_session: Session, schemas: SchemaSet, no_hints: None
) -> None:
    body = tool_body(
        "PostToolUse",
        "Write",
        {"file_path": f"{ws_session.workspace}/a.txt", "content": "hello"},
        tool_response={"type": "create", "filePath": f"{ws_session.workspace}/a.txt"},
        tool_use_id="toolu_01AbC-x",
        duration_ms=12,
    )
    assert hook_client.post("PostToolUse", body).json() == {}
    assert bodies(context, "session.hook") == [{"event": "PostToolUse", "tool": "Write", "decision": "none"}]
    (tool,) = bodies(context, "session.tool")
    assert tool == {
        "tool_use_id": "toolu_01AbC-x",
        "name": "Write",
        "input_summary": f"{ws_session.workspace}/a.txt",
        "input": json.dumps(body["tool_input"], sort_keys=True),
        "result_summary": json.dumps(body["tool_response"], sort_keys=True),
        "is_error": False,
        "duration_ms": 12,
    }
    assert control_hooks.FILE_HINTS.take(ws_session.id) == [f"{ws_session.workspace}/a.txt"]


def test_post_tool_use_of_edit_with_odd_fields(
    hook_client: HookClient, context: ControlContext, ws_session: Session, no_hints: None
) -> None:
    long_result = "r" * (hooks.MAX_TOOL_TEXT + 10)
    body = tool_body("PostToolUse", "Edit", {"file_path": "sub/b.txt"}, tool_response=long_result, tool_use_id="t1")
    body["duration_ms"] = True
    hook_client.post("PostToolUse", body)
    (tool,) = bodies(context, "session.tool")
    assert tool["duration_ms"] == 0
    assert tool["result_summary"] == "r" * hooks.MAX_TOOL_TEXT
    assert control_hooks.FILE_HINTS.take(ws_session.id) == [f"{ws_session.workspace}/sub/b.txt"]


@pytest.mark.parametrize(
    ("tool_input", "tool_use_id", "hint"),
    [
        ({"file_path": "c.txt"}, None, True),
        ({"file_path": "c.txt"}, "bad id!", True),
        ({"file_path": "c.txt"}, "x" * 129, True),
        ({"file_path": 5}, "t2", False),
        ({"file_path": ""}, "t3", False),
        ({"file_path": "c\x00"}, "t4", False),
    ],
)
def test_post_tool_use_partial_records(
    hook_client: HookClient,
    context: ControlContext,
    *,
    ws_session: Session,
    no_hints: None,
    tool_input: dict[str, Any],
    tool_use_id: str | None,
    hint: bool,
) -> None:
    extra = {} if tool_use_id is None else {"tool_use_id": tool_use_id}
    hook_client.post("PostToolUse", tool_body("PostToolUse", "Write", tool_input, **extra))
    tools = bodies(context, "session.tool")
    if tool_use_id in ("t2", "t3", "t4"):
        assert tools[0]["input_summary"] == ("" if tool_use_id == "t2" else tool_input["file_path"])
    else:
        assert tools == []
    assert bool(control_hooks.FILE_HINTS.take(ws_session.id)) is hint


def test_post_tool_use_of_read_records_no_tool(
    hook_client: HookClient, context: ControlContext, ws_session: Session, no_hints: None
) -> None:
    hook_client.post("PostToolUse", tool_body("PostToolUse", "Read", {"file_path": "notes.txt"}, tool_use_id="t"))
    assert bodies(context, "session.tool") == []
    assert control_hooks.FILE_HINTS.take(ws_session.id) == []


# ---------------------------------------------------------------- addons


class AddonServer:
    """An API server whose hook routes use the given addons."""

    def __init__(self, config: GuestConfig, addons: Sequence[tuple[str, HookAddon]]) -> None:
        base = create_server(replace(config, api_port=0))
        base.server_close()
        self.server = ApiHttpServer(replace(config, api_port=0), Router(hooks.routes(addons)), base.context)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()
        self.client = HookClient(int(self.server.server_address[1]))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server.context.control.close()
        self.thread.join(5)


@pytest.fixture
def with_addons(guest_config: GuestConfig, session: Session) -> Iterator[Callable[..., HookClient]]:
    servers: list[AddonServer] = []

    def start(*addons: tuple[str, HookAddon]) -> HookClient:
        servers.append(AddonServer(guest_config, list(addons)))
        return servers[-1].client

    yield start
    for server in servers:
        server.close()


def recording(calls: list[HookCall], text: object = NOTICE, events: Sequence[str] = hooks.EVENTS) -> HookAddon:
    def handle(context: ApiContext, call: HookCall) -> str | None:
        calls.append(call)
        return cast(str | None, text) if call.decorate or call.event == "Stop" else None

    return HookAddon(frozenset(events), handle)


def test_addon_notice_is_attached_to_tool_and_prompt_answers(
    with_addons: Callable[..., HookClient], context: ControlContext, session: Session, schemas: SchemaSet
) -> None:
    calls: list[HookCall] = []
    client = with_addons(("ping", recording(calls)))
    register_prompt(context, session.id, "hi")
    answers = {
        "PreToolUse": client.post("PreToolUse", tool_body("PreToolUse", "Bash", {})).json(),
        "PostToolUse": client.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json(),
        "UserPromptSubmit": client.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="hi")).json(),
    }
    for event, answer in answers.items():
        assert answer["hookSpecificOutput"]["additionalContext"] == NOTICE
        assert answer["hookSpecificOutput"]["hookEventName"] == event
        assert schemas.errors(schema_of(event, "response"), answer) == []
    assert answers["PreToolUse"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert [(call.event, call.decision, call.decorate) for call in calls] == [
        ("PreToolUse", "deny", True),
        ("PostToolUse", "none", True),
        ("UserPromptSubmit", "allow", True),
    ]
    assert calls[0].session_id == session.id
    assert calls[0].body is not None
    assert calls[0].body["tool_name"] == "Bash"
    assert bodies(context, "guest.error") == []


def test_addon_text_is_never_attached_to_a_block_or_to_stop(
    with_addons: Callable[..., HookClient], context: ControlContext, session: Session
) -> None:
    calls: list[HookCall] = []
    client = with_addons(("ping", recording(calls)))
    assert client.post("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="typed")).json() == closed_answer(
        "UserPromptSubmit"
    )
    assert client.post("Stop", hook_body("Stop")).json() == {}
    assert [(call.event, call.decorate) for call in calls] == [("UserPromptSubmit", False), ("Stop", False)]
    # the Stop addon returned text: dropped and reported
    assert bodies(context, "guest.error") == [{"code": "hook_handler_failed"}]


def test_addons_only_see_their_events(with_addons: Callable[..., HookClient], session: Session) -> None:
    calls: list[HookCall] = []
    client = with_addons(("stop", recording(calls, None, ["Stop"])))
    assert client.post("PreToolUse", tool_body("PreToolUse", "Bash", {})).status == 200
    assert client.post("Stop", hook_body("Stop")).json() == {}
    assert [call.event for call in calls] == ["Stop"]


@pytest.mark.parametrize("text", ["not a notice", 5], ids=["schema", "type"])
def test_bad_addon_text_is_dropped(
    with_addons: Callable[..., HookClient], context: ControlContext, session: Session, text: object
) -> None:
    client = with_addons(("bad", recording([], text)))
    answer = client.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json()
    assert answer == {}
    assert bodies(context, "guest.error") == [{"code": "hook_handler_failed"}]


def test_failing_addon_never_changes_the_decision(
    with_addons: Callable[..., HookClient],
    context: ControlContext,
    session: Session,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(context: ApiContext, call: HookCall) -> str | None:
        raise RuntimeError("addon bug")

    calls: list[HookCall] = []
    client = with_addons(("a_boom", HookAddon(frozenset({"PreToolUse"}), boom)), ("b_ping", recording(calls)))
    answer = client.post("PreToolUse", tool_body("PreToolUse", "Bash", {})).json()
    assert answer["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert answer["hookSpecificOutput"]["additionalContext"] == NOTICE
    assert bodies(context, "guest.error") == [{"code": "hook_handler_failed"}]
    assert "hook addon a_boom failed" in capsys.readouterr().err


def test_two_addon_texts_are_joined(with_addons: Callable[..., HookClient], session: Session) -> None:
    client = with_addons(("a", recording([], NOTICE)), ("b", recording([], NOTICE)))
    # two notices joined do not match the single notice of the contract: the answer stays undecorated
    assert client.post("PostToolUse", tool_body("PostToolUse", "Grep", {"pattern": "x"})).json() == {}


def make_addon_package(root: Path, name: str, source: str) -> str:
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "one.py").write_text(source, encoding="utf-8")
    return name


@pytest.mark.parametrize(
    ("source", "ok"),
    [
        ("from agent_guest.hooks import HookAddon\nADDON = HookAddon(frozenset({'Stop'}), lambda c, k: None)\n", True),
        ("ADDON = 1\n", False),
        ("from agent_guest.hooks import HookAddon\nADDON = HookAddon(frozenset({'Evil'}), lambda c, k: None)\n", False),
        ("from agent_guest.hooks import HookAddon\nADDON = HookAddon(frozenset(), lambda c, k: None)\n", False),
    ],
)
def test_load_addons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, ok: bool) -> None:
    monkeypatch.syspath_prepend(str(tmp_path))
    name = make_addon_package(tmp_path, f"addons_{abs(hash(source)) % 10**8}", source)
    try:
        if ok:
            assert [entry[0] for entry in hooks.load_addons(name)] == ["one"]
        else:
            with pytest.raises(registry.RegistryError) as error:
                hooks.load_addons(name)
            assert error.value.code == "registry_invalid"
    finally:
        for module in [key for key in sys.modules if key.startswith(name)]:
            del sys.modules[module]


def test_this_package_has_no_addons_yet() -> None:
    assert hooks.load_addons() == [(name, addon) for name, addon in hooks.load_addons()]


# ---------------------------------------------------------------- fault switch


def golden(tmp_path: Path, content: str | None) -> str:
    path = tmp_path / f"golden-{abs(hash(content)) % 10**8}.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.fixture
def fault_client(guest_config: GuestConfig, session: Session) -> Iterator[Callable[[str, str], HookClient]]:
    servers: list[AddonServer] = []

    def start(fault: str, golden_file: str) -> HookClient:
        config = replace(guest_config, fault_hooks=fault, golden_install_file=golden_file)
        servers.append(AddonServer(config, []))
        return servers[-1].client

    yield start
    for server in servers:
        server.close()


class Ended:
    """Stands in for the session manager of the control process: records the sessions the guest ends."""

    def __init__(self) -> None:
        self.ended: list[tuple[str, str]] = []

    def fail_running(self, session_id: str, reason: str) -> bool:
        self.ended.append((session_id, reason))
        return True


def test_fault_switch_answers_500_in_a_test_golden(
    fault_client: Callable[[str, str], HookClient],
    context: ControlContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ended = Ended()
    monkeypatch.setattr(sessions, "manager", lambda _context: ended)
    client = fault_client("500", golden(tmp_path, '{"test_mode": true, "test_model_port": 18080}'))
    before = hooks.STATS.snapshot()["faults"]
    for event in ("PreToolUse", "UserPromptSubmit", "Stop"):
        reply = client.post(event, tool_body(event, "Bash", {}))
        assert (reply.status, reply.json()) == (500, {"error": "unavailable"})
    assert hooks.STATS.snapshot()["faults"] == before + 3
    # ADR-064 section 2: each failed decision hook ends the session through the control process
    assert ended.ended == [(make_session().id, "hook_unavailable")] * 3
    assert bodies(context, "session.hook")[0] == {"event": "PreToolUse", "decision": "none", "reason": "fault injected"}
    assert len(bodies(context, "guest.error")) == 3
    assert client.post("Stop", hook_body("Stop"), token="wrong").status == 401


@pytest.mark.parametrize(
    "content",
    [None, '{"test_mode": false}', '{"test_mode": 1}', "[true]", "{broken", '{"claude_code": "2.1.330"}'],
    ids=["missing", "false", "not-bool", "not-object", "broken", "production"],
)
def test_fault_switch_has_no_effect_in_a_production_golden(
    fault_client: Callable[[str, str], HookClient], tmp_path: Path, content: str | None
) -> None:
    client = fault_client("500", golden(tmp_path, content))
    reply = client.post("PreToolUse", tool_body("PreToolUse", "Bash", {}))
    assert reply.status == 200
    assert reply.json()["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_fault_switch_off_in_a_test_golden(fault_client: Callable[[str, str], HookClient], tmp_path: Path) -> None:
    client = fault_client("", golden(tmp_path, '{"test_mode": true}'))
    assert client.post("Stop", hook_body("Stop")).status == 200


def test_fault_switch_configuration(guest_env: dict[str, str]) -> None:
    config = load_config(guest_env)
    assert (config.fault_hooks, config.golden_install_file) == ("", "/var/lib/agent-golden/install.json")
    assert load_config({**guest_env, "AGENT_GUEST_FAULT_HOOKS": "500"}).fault_hooks == "500"
    for name, value in (("FAULT_HOOKS", "1"), ("FAULT_HOOKS", "503"), ("GOLDEN_INSTALL_FILE", "relative.json")):
        with pytest.raises(ConfigError) as error:
            load_config({**guest_env, f"AGENT_GUEST_{name}": value})
        assert error.value.name == name


# ---------------------------------------------------------------- control process unreachable


class FakeControl:
    """Stands in for ``ApiClient``: resolves the session, then fails the chosen op."""

    def __init__(self, fail: str, session: dict[str, Any] | None = None) -> None:
        self.fail = fail
        self.session = session or {"id": make_session().id, "workspace": "/srv/sessions/x/work"}
        self.appended: list[tuple[str, dict[str, Any]]] = []

    def resolve_session(self, token: str | None, peer_uid: int | None = None) -> dict[str, Any] | None:
        if self.fail == "resolve":
            raise ApiSockError("unavailable")
        return self.session

    def append(self, kind: str, body: dict[str, Any], session_id: str | None = None) -> int:
        if self.fail in ("append", "all"):
            raise ApiSockError("unavailable")
        self.appended.append((kind, body))
        return 1

    def call(self, op: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        raise ApiSockError("unavailable")


def direct(
    guest_config: GuestConfig, schemas: SchemaSet, control: FakeControl, event: str, body: dict[str, Any]
) -> object:
    context = ApiContext(config=guest_config, schemas=schemas, control=cast(ApiClient, control))
    data = json.dumps(body).encode()
    headers = Message()
    headers["Authorization"] = f"Bearer {SESSION_TOKEN}"
    request = Request("POST", f"/hooks/{event}", {}, headers, stream=BodyReader(io.BytesIO(data), len(data)))
    return hooks.handle(context, request, event, [])


@pytest.mark.parametrize(
    ("fail", "body"),
    [("resolve", hook_body("Stop")), ("append", hook_body("Stop"))],
)
def test_unreachable_control_process_answers_503(
    guest_config: GuestConfig, schemas: SchemaSet, fail: str, body: dict[str, Any]
) -> None:
    control = FakeControl(fail)
    before = hooks.STATS.snapshot()["errors"]
    with pytest.raises(HttpError) as error:
        direct(guest_config, schemas, control, "Stop", body)
    assert (error.value.code, error.value.status) == ("unavailable", 503)
    assert control.appended == []
    assert hooks.STATS.snapshot()["errors"] == before + 1


FAILED_OP = [("guest.error", {"code": "hook_handler_failed"})]


@pytest.mark.parametrize(
    ("fail", "event", "body", "appended"),
    [
        ("resolve", "PreToolUse", tool_body("PreToolUse", "Read", {}), []),
        ("resolve", "UserPromptSubmit", hook_body("UserPromptSubmit", prompt="x"), []),
        ("op", "PreToolUse", tool_body("PreToolUse", "Read", {}), FAILED_OP),
        ("op", "UserPromptSubmit", hook_body("UserPromptSubmit", prompt="x"), FAILED_OP),
        ("all", "PreToolUse", {"broken": True}, []),
    ],
)
def test_decisions_fail_closed_when_the_control_process_fails(
    guest_config: GuestConfig,
    schemas: SchemaSet,
    *,
    fail: str,
    event: str,
    body: dict[str, Any],
    appended: list[tuple[str, dict[str, Any]]],
) -> None:
    control = FakeControl(fail)
    before = hooks.STATS.snapshot()["errors"]
    response = cast(Response, direct(guest_config, schemas, control, event, body))
    assert (response.status, json.loads(cast(bytes, response.body))) == (200, closed_answer(event))
    assert control.appended == appended
    assert hooks.STATS.snapshot()["errors"] == before + 1


def test_unauthorized_calls_are_counted(hook_client: HookClient, session: Session) -> None:
    before = hooks.STATS.snapshot()["unauthorized"]
    assert hook_client.post("Stop", hook_body("Stop"), token="nope").status == 401
    assert hooks.STATS.snapshot()["unauthorized"] == before + 1


@pytest.mark.parametrize(
    ("event", "body"),
    [
        ("PreToolUse", tool_body("PreToolUse", "Read", {"file_path": "/etc/\ud800/../passwd"})),
        ("PreToolUse", tool_body("PreToolUse", "Grep", {"path": "/etc", "pattern": "root|\ud800"})),
        ("UserPromptSubmit", hook_body("UserPromptSubmit", prompt="hi \ud800")),
    ],
)
def test_lone_surrogates_fail_closed(
    hook_client: HookClient, context: ControlContext, session: Session, event: str, body: dict[str, Any]
) -> None:
    reply = hook_client.post(event, raw=json.dumps(body).encode("ascii"))
    assert (reply.status, reply.json()) == (200, closed_answer(event))
    assert len(bodies(context, "session.hook")) == 1


def test_large_oversized_body_is_drained_and_denied(
    hook_client: HookClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_MAX_BODY", 64)
    body = tool_body("PreToolUse", "Write", {"file_path": "a.txt", "content": "x" * (4 * 1024 * 1024)})
    reply = hook_client.post("PreToolUse", body)
    assert (reply.status, reply.json()) == (200, closed_answer("PreToolUse"))


def test_huge_body_is_not_drained(
    guest_config: GuestConfig, schemas: SchemaSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hooks, "HOOK_MAX_BODY", 8)
    monkeypatch.setattr(hooks, "DRAIN_MAX_BODY", 16)
    control = FakeControl("none")
    response = cast(Response, direct(guest_config, schemas, control, "Stop", {"padding": "x" * 64}))
    assert json.loads(cast(bytes, response.body)) == {}
    assert control.appended == [("session.hook", {"event": "Stop", "decision": "none", "reason": "hook body invalid"})]


# ---------------------------------------------------------------- control-side ops


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("hook.pre_tool_use", {"session_id": make_session().id, "tool_name": "Read"}),
        ("hook.pre_tool_use", {"session_id": make_session().id, "tool_name": 1, "tool_input": {}}),
        ("hook.pre_tool_use", {"session_id": make_session().id, "tool_name": "Read", "tool_input": []}),
        ("hook.pre_tool_use", {"session_id": make_session().id, "tool_name": "Read", "tool_input": {"content": "x"}}),
        ("hook.post_tool_use", {"session_id": make_session().id, "body": {"name": "Read"}}),
        ("hook.post_tool_use", {"session_id": make_session().id, "body": []}),
        ("hook.post_tool_use", {"session_id": make_session().id}),
        ("hook.post_tool_use", {"session_id": make_session().id, "body": {"name": "Write"}}),
        ("file.changed", {"session_id": make_session().id, "path": "relative"}),
        ("file.changed", {"session_id": make_session().id, "path": "/a\x00"}),
        ("file.changed", {"session_id": make_session().id, "path": "/" + "a" * 4096}),
        ("file.changed", {"session_id": make_session().id, "path": 1}),
        ("file.changed", {"session_id": make_session().id}),
    ],
)
def test_control_ops_refuse_malformed_arguments(
    api_client: ApiClient, session: Session, op: str, args: dict[str, Any]
) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call(op, args)
    assert error.value.code == "request_invalid"


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("hook.pre_tool_use", {"session_id": OTHER_SESSION_ID, "tool_name": "Read", "tool_input": {}}),
        ("hook.post_tool_use", {"session_id": 5, "body": {"name": "Write"}}),
        ("file.changed", {"session_id": OTHER_SESSION_ID, "path": "/a"}),
    ],
)
def test_control_ops_refuse_unknown_sessions(
    api_client: ApiClient, session: Session, op: str, args: dict[str, Any]
) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call(op, args)
    assert error.value.code == "not_found"


def test_control_ops_report_an_unavailable_journal(guest_config: GuestConfig) -> None:
    def append(kind: str, body: dict[str, Any], session_id: str | None = None) -> int:
        raise JournalError("unavailable")

    session = make_session()
    context = SimpleNamespace(
        state=SimpleNamespace(session=lambda session_id: session), journal=SimpleNamespace(append=append)
    )
    with pytest.raises(JournalError) as error:
        control_hooks.pre_tool_use(
            cast(ControlContext, context), {"session_id": session.id, "tool_name": "Bash", "tool_input": {}}
        )
    assert error.value.code == "unavailable"


def test_file_hints_are_bounded_and_unique() -> None:
    hints = control_hooks.FileHints(limit=2)
    for path in ("/a", "/a", "/b", "/c"):
        hints.add("s", path)
    assert hints.take("s") == ["/a", "/b"]
    assert hints.take("s") == []


def test_file_changed_helper(api_client: ApiClient, session: Session, no_hints: None) -> None:
    hooks.file_changed(api_client, session.id, f"{session.workspace}/a.txt")
    assert control_hooks.FILE_HINTS.take(session.id) == [f"{session.workspace}/a.txt"]
    for outside in (f"{session.workspace}/../x", session.workspace, "/etc/passwd"):
        with pytest.raises(ApiSockError) as error:
            hooks.file_changed(api_client, session.id, outside)
        assert error.value.code == "request_invalid"


def test_registry_has_the_hook_ops() -> None:
    assert {"hook.pre_tool_use", "hook.post_tool_use", "file.changed", "prompt.check"} <= set(registry.api_ops())
    paths = [(route.method, route.path) for route in registry.control_routes()]
    assert ("POST", "/v1/sessions/{id}/prompts") in paths


def test_bearer_parsing() -> None:
    def headers(*values: str) -> Message:
        message = Message()
        for value in values:
            message["Authorization"] = value
        return message

    assert hooks.bearer(headers("Bearer abc")) == "abc"
    assert hooks.bearer(headers()) is None
    assert hooks.bearer(headers("Bearer a", "Bearer a")) is None
    assert hooks.bearer(headers("Token abc")) is None


def test_state_error_codes_are_known() -> None:
    assert StateError("not_found").code == "not_found"
