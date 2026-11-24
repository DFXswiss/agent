"""U-GST-hooks, fail closed (ADR-064 section 2): a failing decision hook is reported to the control process, which
ends the session; the other events only journal; the fault switch ``open`` suppresses the reaction."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any, cast

import pytest

from agent_guest import fault, hooks
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.config import GuestConfig, load_config
from agent_guest.hooks import HookAddon, HookCall
from agent_guest.routing import BodyReader, HttpError, Request, Response
from agent_guest.sandbox import settings
from agent_guest.schema import SchemaSet
from agent_guest.server import ApiContext
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN
from tests.platform.guest.hooks.conftest import hook_body, tool_body

FAILED = ("session.hook_failed",)


class Control:
    """Stands in for ``ApiClient``: answers the decision ops, records every call, fails or raises on request."""

    def __init__(self, *, fail: tuple[str, ...] = (), raise_on: tuple[str, ...] = ()) -> None:
        self.fail, self.raise_on = fail, raise_on
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.appended: list[tuple[str, dict[str, Any]]] = []

    def resolve_session(self, token: str | None, peer_uid: int | None = None) -> dict[str, Any]:
        return {"id": SESSION_ID, "workspace": f"/srv/sessions/{SESSION_ID}/work"}

    def append(self, kind: str, body: dict[str, Any], session_id: str | None = None) -> int:
        self.appended.append((kind, body))
        return 1

    def call(self, op: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append((op, dict(args or {})))
        if op in self.fail:
            raise ApiSockError("unavailable")
        if op in self.raise_on:
            raise RuntimeError("broken")
        answers: dict[str, dict[str, Any]] = {
            "prompt.check": {"accepted": True},
            "hook.pre_tool_use": {"decision": "allow"},
        }
        return answers.get(op, {})

    def reported(self) -> list[dict[str, Any]]:
        return [args for op, args in self.calls if op == "session.hook_failed"]


def golden(tmp_path: Path, test_mode: bool) -> str:
    path = tmp_path / f"install-{test_mode}.json"
    path.write_text(json.dumps({"test_mode": test_mode}), encoding="utf-8")
    return str(path)


def handle(
    config: GuestConfig,
    schemas: SchemaSet,
    control: Control,
    event: str,
    body: dict[str, Any],
    *,
    addons: list[tuple[str, HookAddon]] | None = None,
) -> Response:
    context = ApiContext(config=config, schemas=schemas, control=cast(ApiClient, control))
    data = json.dumps(body).encode()
    headers = Message()
    headers["Authorization"] = f"Bearer {SESSION_TOKEN}"
    request = Request("POST", f"/hooks/{event}", {}, headers, stream=BodyReader(io.BytesIO(data), len(data)))
    return hooks.handle(context, request, event, addons or [])


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The handler's monotonic clock: each call takes the next value (the last one repeats)."""
    values = [0.0]

    def now() -> float:
        return values.pop(0) if len(values) > 1 else values[0]

    monkeypatch.setattr(hooks, "MONOTONIC", now)
    return values


# ---------------------------------------------------------------- the fault switch


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse", "Stop", "StopFailure"])
def test_switch_500_reports_every_decision_hook(
    guest_config: GuestConfig, schemas: SchemaSet, tmp_path: Path, event: str
) -> None:
    config = replace(guest_config, fault_hooks="500", golden_install_file=golden(tmp_path, True))
    control = Control()
    response = handle(config, schemas, control, event, tool_body(event, "Read", {}))
    assert response.status == 500
    assert control.reported() == [{"session_id": SESSION_ID, "event": event}]
    assert ("guest.error", {"code": "hook_handler_failed"}) in control.appended


def test_switch_500_does_not_report_other_hooks(guest_config: GuestConfig, schemas: SchemaSet, tmp_path: Path) -> None:
    config = replace(guest_config, fault_hooks="500", golden_install_file=golden(tmp_path, True))
    control = Control()
    assert handle(config, schemas, control, "Notification", hook_body("Notification")).status == 500
    assert control.reported() == []


def test_switch_open_answers_500_without_the_reaction(
    guest_config: GuestConfig, schemas: SchemaSet, tmp_path: Path
) -> None:
    config = replace(guest_config, fault_hooks="open", golden_install_file=golden(tmp_path, True))
    control = Control()
    response = handle(config, schemas, control, "UserPromptSubmit", hook_body("UserPromptSubmit", prompt="x"))
    assert response.status == 500
    assert control.reported() == []


@pytest.mark.parametrize("value", ["500", "open"])
def test_the_switch_needs_a_test_golden(guest_config: GuestConfig, tmp_path: Path, value: str) -> None:
    production = replace(guest_config, fault_hooks=value, golden_install_file=golden(tmp_path, False))
    assert fault.fault_mode(production) is None
    assert not fault.guard_suppressed(production)
    test = replace(production, golden_install_file=golden(tmp_path, True))
    assert fault.fault_mode(test) == value
    assert fault.guard_suppressed(test) is (value == "open")
    assert fault.fault_mode(replace(test, fault_hooks="")) is None


def test_the_switch_accepts_open(guest_env: dict[str, str]) -> None:
    assert load_config({**guest_env, "AGENT_GUEST_FAULT_HOOKS": "open"}).fault_hooks == "open"


# ---------------------------------------------------------------- failures of the hook path


def test_a_failed_decision_op_is_reported_and_answered_closed(guest_config: GuestConfig, schemas: SchemaSet) -> None:
    control = Control(fail=("prompt.check",))
    response = handle(guest_config, schemas, control, "UserPromptSubmit", hook_body("UserPromptSubmit", prompt="x"))
    assert json.loads(cast(bytes, response.body)) == hooks.closed_answer("UserPromptSubmit")
    assert control.reported() == [{"session_id": SESSION_ID, "event": "UserPromptSubmit"}]


def test_an_unexpected_error_of_a_decision_is_reported_and_answered_closed(
    guest_config: GuestConfig, schemas: SchemaSet, capsys: pytest.CaptureFixture[str]
) -> None:
    control = Control(raise_on=("hook.pre_tool_use",))
    response = handle(guest_config, schemas, control, "PreToolUse", tool_body("PreToolUse", "Read", {}))
    assert json.loads(cast(bytes, response.body)) == hooks.closed_answer("PreToolUse")
    assert control.reported() == [{"session_id": SESSION_ID, "event": "PreToolUse"}]
    assert "hook PreToolUse failed" in capsys.readouterr().err


def test_an_unexpected_error_of_another_hook_is_503_without_a_report(
    guest_config: GuestConfig, schemas: SchemaSet
) -> None:
    control = Control(raise_on=("hook.post_tool_use",))
    body = tool_body("PostToolUse", "Write", {"file_path": "a.txt", "content": "x"})
    body.update({"tool_response": "ok", "tool_use_id": "toolu_1"})
    with pytest.raises(HttpError) as error:
        handle(guest_config, schemas, control, "PostToolUse", body)
    assert error.value.code == "unavailable"
    assert control.reported() == []
    assert ("guest.error", {"code": "hook_handler_failed"}) in control.appended


def test_an_unreachable_control_process_keeps_the_report_silent(guest_config: GuestConfig, schemas: SchemaSet) -> None:
    control = Control(fail=("prompt.check", "session.hook_failed"))
    response = handle(guest_config, schemas, control, "UserPromptSubmit", hook_body("UserPromptSubmit", prompt="x"))
    assert response.status == 200
    assert len(control.reported()) == 1


def test_an_answer_claude_code_gave_up_on_is_a_failure(
    guest_config: GuestConfig, schemas: SchemaSet, clock: list[float]
) -> None:
    clock[:] = [100.0, 100.0 + settings.HOOK_TIMEOUT_SECONDS - hooks.LATE_MARGIN_SECONDS]
    control = Control()
    response = handle(guest_config, schemas, control, "PreToolUse", tool_body("PreToolUse", "Read", {}))
    assert response.status == 200
    assert control.reported() == [{"session_id": SESSION_ID, "event": "PreToolUse"}]
    assert ("guest.error", {"code": "hook_handler_failed"}) in control.appended


def test_an_answer_in_time_is_no_failure(guest_config: GuestConfig, schemas: SchemaSet, clock: list[float]) -> None:
    clock[:] = [100.0, 100.0 + settings.HOOK_TIMEOUT_SECONDS - hooks.LATE_MARGIN_SECONDS - 0.5]
    control = Control()
    handle(guest_config, schemas, control, "PreToolUse", tool_body("PreToolUse", "Read", {}))
    assert control.reported() == []


class Broken:
    def __init__(self, events: frozenset[str]) -> None:
        self.events = events

    def handle(self, context: ApiContext, call: HookCall) -> str | None:
        raise RuntimeError("addon broke")


def test_a_failing_addon_of_stop_is_a_failure_of_the_hook(guest_config: GuestConfig, schemas: SchemaSet) -> None:
    control = Control()
    addon = cast(HookAddon, Broken(frozenset({"Stop", "PreToolUse"})))
    handle(guest_config, schemas, control, "Stop", hook_body("Stop"), addons=[("broken", addon)])
    assert control.reported() == [{"session_id": SESSION_ID, "event": "Stop"}]
    control = Control()
    body = tool_body("PreToolUse", "Read", {})
    handle(guest_config, schemas, control, "PreToolUse", body, addons=[("broken", addon)])
    assert control.reported() == []
