from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import main as main_mod
from agent_cli.drivers import (
    DRIVERS_NOT_CONFIGURED,
    SESSION_ENV_NOT_CONFIGURED,
    lane_scrub_env,
    role_clear_env,
)
from agent_cli.main import apply_control, main, should_sync_on_ws
from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store, StoreError
from driver_fixtures import (
    CONFIG_DIRS,
    accounts_document,
    driver_document,
    driver_specs,
    write_accounts,
    write_drivers,
)

OPERATOR_INTERACTIVE_MODEL = "operator-interactive-model"
OPERATOR_INTERACTIVE_MODEL_B = "operator-interactive-model-b"
OPERATOR_HOME_A = "/operator/path/interactive-a"
OPERATOR_HOME_B = "/operator/path/interactive-b"
SESSION_A = "sess-1"
SESSION_B = "sess-2"


def write_operator_interactive_ai_accounts(
    home: Path,
    *,
    sessions: dict | None = None,
    model: str = OPERATOR_INTERACTIVE_MODEL,
    config_dir: str = OPERATOR_HOME_A,
    account_name: str = "interactive-account-a",
    role_name: str = "interactive-builder",
    access: str = "workspace-write",
) -> None:
    """Write operator-supplied ai-accounts.json for positive interactive starts.

    Not installed by default. Call explicitly for positive interactive provider cases;
    raw-shell starts (provider=None) must not rely on this helper.
    """
    data = {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "driver-a", "second": "driver-b"},
        "accounts": {
            account_name: {"provider": "driver-a", "config_dir": config_dir},
            "interactive-account-b": {
                "provider": "driver-a",
                "config_dir": OPERATOR_HOME_B,
            },
            "interactive-driver-b": {
                "provider": "driver-b",
                "config_dir": "/operator/path/interactive-driver-b",
            },
        },
        "roles": {
            role_name: {
                "account": account_name,
                "model": model,
                "access": access,
            },
            "interactive-builder-b": {
                "account": "interactive-account-b",
                "model": OPERATOR_INTERACTIVE_MODEL_B,
                "access": "workspace-write",
            },
            "interactive-driver-b-role": {
                "account": "interactive-driver-b",
                "model": "operator-driver-b-model",
                "access": "workspace-write",
            },
            "interactive-reader": {
                "account": account_name,
                "model": model,
                "access": "read-only",
            },
        },
        "sessions": sessions
        or {
            SESSION_A: {"interactive": role_name, "lanes": {}},
            SESSION_B: {"interactive": "interactive-builder-b", "lanes": {}},
            "s1": {"interactive": role_name, "lanes": {}},
        },
    }
    write_drivers(home)
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def expected_ai_binding(
    *,
    role: str = "interactive-builder",
    account: str = "interactive-account-a",
    model: str = OPERATOR_INTERACTIVE_MODEL,
    config_dir: str = OPERATOR_HOME_A,
    access: str = "workspace-write",
) -> dict:
    return {
        "role": role,
        "account": account,
        "provider": "driver-a",
        "model": model,
        "access": access,
        "configuration": hashlib.sha256(config_dir.encode()).hexdigest(),
    }


def bind_interactive_runtime(home: Path, session_id: str) -> None:
    store = Store(home)
    try:
        row = store.row("session", session_id)
        assert row is not None
        row["runtime"] = {
            "control": "attached",
            "tmux_session": f"agent-{session_id}",
            "tmux_pane": f"agent-{session_id}:0.0",
            "provider": "driver-a",
            "model": OPERATOR_INTERACTIVE_MODEL,
            "interactive_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "ai_binding": expected_ai_binding(),
        }
        store.write(
            "session",
            "update",
            session_id,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()


def remove_ai_setting(home: Path, setting: str) -> None:
    path = home / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop(setting)
    path.write_text(json.dumps(data), encoding="utf-8")


def run(home: Path, argv: list[str]) -> None:
    import os

    os.environ["AGENT_HOME"] = str(home)
    main(argv)


def _fake_runtime_factory(calls: list[list[str]] | None = None):
    log = calls if calls is not None else []

    def runner(argv: list[str]) -> Completed:
        log.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            if any(c[:2] == ["tmux", "new-session"] for c in log[:-1]):
                return Completed(0, "", "")
            return Completed(1, "", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, "pane-bytes", "")
        return Completed(0, "", "")

    def factory(*_a: object, **_k: object) -> Runtime:
        return Runtime(runner=runner)

    return factory, log


def test_cli_start_provider_mints_uuid_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            if any(c[:2] == ["tmux", "new-session"] for c in calls[:-1]) and not any(
                c[:2] == ["tmux", "kill-session"] for c in calls[:-1]
            ):
                return Completed(0, "", "")
            return Completed(1, "", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    out = capsys.readouterr().out
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        gid = row["runtime"]["interactive_session_id"]
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", gid)
        assert f"interactive={gid}" in out
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
        assert row["runtime"]["model"] == OPERATOR_INTERACTIVE_MODEL
        assert "interactive_start_pending" not in row["runtime"]
        first = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
        assert "env" in first
        assert "TOOL_C_API_KEY" in first
        assert "TOOL_A_API_KEY" in first
        assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in first
        assert "--session-id" in first
        assert gid in first
        assert "--model" in first and OPERATOR_INTERACTIVE_MODEL in first
        assert first[first.index("--model") + 1] == OPERATOR_INTERACTIVE_MODEL
        assert first[first.index("--session-id") + 1] != SESSION_A
        # Nested role env_prefix precedes the existing interactive_command_argv env strip.
        assert first[first.index("--") + 1] == "env"
        second_env = first.index("env", first.index("--") + 2)
        assert second_env > first.index(f"TOOL_A_HOME={OPERATOR_HOME_A}")
    finally:
        store.close()

    run(tmp_path, ["session", "stop", "--id", SESSION_A])
    calls.clear()
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    resume = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
    assert "--resume" in resume
    assert "--session-id" not in resume
    assert OPERATOR_INTERACTIVE_MODEL in resume
    assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in resume
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert resume[resume.index("--resume") + 1] == row["runtime"]["interactive_session_id"]
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
    finally:
        store.close()


def test_cli_start_provider_replaces_bare_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            created = [c for c in calls[:-1] if c[:2] == ["tmux", "new-session"]]
            killed = [c for c in calls[:-1] if c[:2] == ["tmux", "kill-session"]]
            return Completed(0, "", "") if len(created) > len(killed) else Completed(1, "", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    # Raw-shell start: provider=None must not require AI accounts.
    run(tmp_path, ["session", "start", "--id", SESSION_A])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert any(c[:2] == ["tmux", "kill-session"] for c in calls)
    interactive_news = [c for c in calls if c[:2] == ["tmux", "new-session"] and "tool-a" in c]
    assert len(interactive_news) == 1
    assert "--session-id" in interactive_news[0]
    assert OPERATOR_INTERACTIVE_MODEL in interactive_news[0]
    store = Store(tmp_path)
    try:
        gid = store.row("session", SESSION_A)["runtime"]["interactive_session_id"]
        assert gid in interactive_news[0]
        assert store.row("session", SESSION_A)["runtime"]["ai_binding"] == expected_ai_binding()
    finally:
        store.close()


def test_cli_provider_and_cmd_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    factory, _ = _fake_runtime_factory()
    monkeypatch.setattr(main_mod, "Runtime", factory)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    with pytest.raises(SystemExit, match="cannot be used together"):
        run(
            tmp_path,
            ["session", "start", "--id", SESSION_A, "--provider", "driver-a", "--cmd", "bash"],
        )


def test_cli_start_owned_writes_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _ = _fake_runtime_factory()
    monkeypatch.setattr(main_mod, "Runtime", factory)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    # Raw-shell path: no AI accounts required.
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--cols", "80", "--rows", "24"])
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        rt = row.get("runtime")
        assert isinstance(rt, dict)
        assert rt["control"] == "attached"
        assert rt["tmux_session"] == "agent-sess-1"
        assert rt["cols"] == 80
        assert rt["rows"] == 24
        assert "ai_binding" not in rt
        assert "interactive_session_id" not in rt
    finally:
        store.close()


def test_cli_start_foreign_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _ = _fake_runtime_factory()
    monkeypatch.setattr(main_mod, "Runtime", factory)
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "sess-f",
                "payload": {"id": "sess-f", "kind": "human", "status": "active"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
    finally:
        store.close()
    with pytest.raises(SystemExit, match="another device"):
        run(tmp_path, ["session", "start", "--id", "sess-f"])


def test_apply_control_start_stop_input(tmp_path: Path) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s1", "--kind", "human"])
    store = Store(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            if any(c[:2] == ["tmux", "new-session"] for c in calls[:-1]):
                if any(c[:2] == ["tmux", "kill-session"] for c in calls[:-1]):
                    return Completed(1, "", "")
                return Completed(0, "", "")
            return Completed(1, "", "")
        return Completed(0, "", "")

    runtime = Runtime(runner=runner)
    try:
        ack = apply_control(
            store,
            runtime,
            {
                "type": "control",
                "session_id": "s1",
                "action": "start",
                "payload": {"provider": "driver-a", "cols": 80, "rows": 24},
            },
        )
        assert ack["ok"] is True
        row = store.row("session", "s1")
        assert row is not None
        assert row["runtime"]["provider"] == "driver-a"
        assert row["runtime"]["model"] == OPERATOR_INTERACTIVE_MODEL
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            row["runtime"]["interactive_session_id"],
        )
        interactive_new = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
        assert "--session-id" in interactive_new
        assert OPERATOR_INTERACTIVE_MODEL in interactive_new
        assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in interactive_new
        assert interactive_new[interactive_new.index("--model") + 1] == OPERATOR_INTERACTIVE_MODEL

        # Non-provider restart keeps the existing attached session (raw resize path).
        ack = apply_control(
            store,
            runtime,
            {"type": "control", "session_id": "s1", "action": "start", "payload": {"cols": 80, "rows": 24}},
        )
        assert ack["ok"] is True
        assert ack["type"] == "control-ack"
        assert ack["action"] == "start"
        row = store.row("session", "s1")
        assert row is not None
        assert row["runtime"]["control"] == "attached"
        assert row["runtime"]["tmux_session"] == "agent-s1"

        ack = apply_control(
            store,
            runtime,
            {"type": "control", "session_id": "s1", "action": "input", "payload": {"data": "hi"}},
        )
        assert ack["ok"] is True
        assert ["tmux", "send-keys", "-t", "agent-s1", "-l", "--", "hi"] in calls

        ack = apply_control(
            store,
            runtime,
            {"type": "control", "session_id": "s1", "action": "stop", "payload": {}},
        )
        assert ack["ok"] is True
        row = store.row("session", "s1")
        assert row is not None
        assert row["runtime"]["control"] == "stopped"
        assert row["runtime"]["tmux_session"] == "agent-s1"
    finally:
        store.close()


def test_apply_control_provider_and_command_not_ok(tmp_path: Path) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s1", "--kind", "human"])
    store = Store(tmp_path)
    runtime = Runtime(runner=lambda argv: Completed(0, "tmux 3.3a", "") if argv[:2] == ["tmux", "-V"] else Completed(1, "", ""))
    try:
        ack = apply_control(
            store,
            runtime,
            {
                "type": "control",
                "session_id": "s1",
                "action": "start",
                "payload": {"provider": "driver-a", "command": "bash"},
            },
        )
        assert ack["ok"] is False
        assert "together" in (ack.get("error") or "")
    finally:
        store.close()


def test_apply_control_foreign_not_ok(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "session",
                "op": "insert",
                "row_id": "sess-f",
                "payload": {"id": "sess-f", "kind": "human", "status": "active"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        runtime = Runtime(runner=lambda argv: Completed(0, "", ""))
        ack = apply_control(
            store,
            runtime,
            {"type": "control", "session_id": "sess-f", "action": "start", "payload": {}},
        )
        assert ack["ok"] is False
        assert "another device" in (ack.get("error") or "")
    finally:
        store.close()


def test_apply_control_bad_quoting_acks_false(tmp_path: Path) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s1", "--kind", "human"])
    store = Store(tmp_path)
    runtime = Runtime(runner=lambda argv: Completed(0, "tmux 3.3a", "") if argv[:2] == ["tmux", "-V"] else Completed(1, "", ""))
    try:
        ack = apply_control(
            store,
            runtime,
            {"type": "control", "session_id": "s1", "action": "start", "payload": {"command": "'"}},
        )
        assert ack["ok"] is False
        assert "quoting" in (ack.get("error") or "")
    finally:
        store.close()


def test_should_sync_on_ws_false_for_control_messages() -> None:
    for msg_type in ("control", "terminal", "control-ack", "control-ready"):
        assert should_sync_on_ws({"type": msg_type}) is False


def test_missing_interactive_account_refuses_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Genuinely empty AI config: a provider start must not start tmux or the driver."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    with pytest.raises(SystemExit, match="drivers is not configured: drivers.json"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    write_drivers(tmp_path)
    with pytest.raises(SystemExit, match="No AI session configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)


def test_missing_models_refuses_after_interactive_binding_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    path = tmp_path / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("models")
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        return Completed(1, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match="models is not configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)


def test_missing_lane_execution_refuses_interactive_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    path = tmp_path / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("lane_execution")
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match="lane_execution is not configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_interactive_configuration_is_checked_before_runtime_target_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    path = tmp_path / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop(missing)
    path.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "runtime-targets.json").write_text(
        json.dumps({SESSION_A: ["remote-runtime"]}), encoding="utf-8"
    )
    calls: list[list[str]] = []

    def runtime_factory(*_args: object, **kwargs: object) -> Runtime:
        targets = kwargs.get("tmux_targets")
        assert targets == {SESSION_A: ["remote-runtime"]}
        assert isinstance(targets, dict)
        return Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", ""),
            tmux_targets=targets,
        )

    monkeypatch.setattr(main_mod, "Runtime", runtime_factory)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match=f"{missing} is not configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    assert calls == []


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_fresh_interactive_start_rechecks_configuration_between_runtime_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            remove_ai_setting(tmp_path, missing)
            return Completed(0, "tmux 3.3a", "")
        return Completed(1, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match=f"{missing} is not configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    assert calls == [["tmux", "-V"]]


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_fresh_interactive_start_rechecks_configuration_before_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []
    has_session_calls = 0

    def runner(argv: list[str]) -> Completed:
        nonlocal has_session_calls
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            has_session_calls += 1
            if has_session_calls == 2:
                remove_ai_setting(tmp_path, missing)
            return Completed(1, "", "session not found")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match=f"{missing} is not configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    assert has_session_calls == 2
    assert not any(argv[:2] == ["tmux", "new-session"] for argv in calls)


def test_fresh_interactive_start_keeps_metadata_when_session_appears_between_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []
    has_session_calls = 0

    def runner(argv: list[str]) -> Completed:
        nonlocal has_session_calls
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            has_session_calls += 1
            if has_session_calls == 1:
                return Completed(1, "", "session not found")
            return Completed(0, "", "")
        if argv[:2] == ["tmux", "new-session"]:
            pytest.fail("the concurrent tmux session must not be replaced")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    original_runtime = {
        "tmux_session": f"agent-{SESSION_A}",
        "control": "stopped",
        "keep_working": {"standing_sent": True},
    }
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = original_runtime
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    with pytest.raises(
        SystemExit,
        match="tmux session already exists; the interactive driver was not started",
    ):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert row["runtime"] == original_runtime
    finally:
        store.close()
    assert has_session_calls == 2
    assert not any(argv[:2] == ["tmux", "new-session"] for argv in calls)
    assert not any(argv[:2] == ["tmux", "resize-window"] for argv in calls)


def test_failed_interactive_start_marker_write_does_not_start_or_change_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "Runtime",
        lambda *a, **k: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    original_runtime = {
        "tmux_session": f"agent-{SESSION_A}",
        "control": "stopped",
        "keep_working": {"standing_sent": True},
    }
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = original_runtime
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    original_write = Store.write

    def fail_start_marker_write(
        self: Store, table: str, op: str, row_id: str, payload: dict
    ) -> dict:
        runtime = payload.get("runtime")
        if (
            table == "session"
            and op == "update"
            and row_id == SESSION_A
            and isinstance(runtime, dict)
            and runtime.get("interactive_start_pending") is True
        ):
            raise StoreError("start marker write failed")
        return original_write(self, table, op, row_id, payload)

    monkeypatch.setattr(Store, "write", fail_start_marker_write)

    with pytest.raises(SystemExit, match="start marker write failed"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    assert calls == []
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert row["runtime"] == original_runtime
    finally:
        store.close()


def test_failed_interactive_new_session_restores_original_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(1, "", "session not found")
        if argv[:2] == ["tmux", "new-session"]:
            return Completed(1, "", "new-session failed")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    original_runtime = {
        "tmux_session": f"agent-{SESSION_A}",
        "control": "stopped",
        "keep_working": {"standing_sent": True},
    }
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = original_runtime
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="new-session failed"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    assert any(argv[:2] == ["tmux", "new-session"] for argv in calls)
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert row["runtime"] == original_runtime
    finally:
        store.close()


def test_failed_final_interactive_metadata_write_keeps_binding_and_rejects_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(1, "", "session not found")
        return Completed(0, "", "")

    original_write = Store.write

    def fail_final_metadata_write(
        self: Store, table: str, op: str, row_id: str, payload: dict
    ) -> dict:
        runtime = payload.get("runtime")
        if (
            table == "session"
            and op == "update"
            and row_id == SESSION_A
            and isinstance(runtime, dict)
            and runtime.get("provider") == "driver-a"
            and runtime.get("interactive_start_pending") is not True
            and any(argv[:2] == ["tmux", "new-session"] for argv in calls)
        ):
            raise StoreError("final metadata write failed")
        return original_write(self, table, op, row_id, payload)

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    monkeypatch.setattr(Store, "write", fail_final_metadata_write)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match="final metadata write failed"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert row["runtime"]["interactive_start_pending"] is True
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
        assert "interactive_session_id" not in row["runtime"]
    finally:
        store.close()

    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    raw["roles"]["interactive-builder"]["model"] = "changed-after-failed-write"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")
    calls.clear()
    with pytest.raises(SystemExit, match="interactive AI binding changed"):
        run(tmp_path, ["session", "input", "--id", SESSION_A, "--data", "continue"])
    assert calls == []


def test_providerless_start_preflights_existing_interactive_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    remove_ai_setting(tmp_path, "models")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "Runtime",
        lambda *a, **k: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )

    with pytest.raises(SystemExit, match="models is not configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A])

    assert calls == []


def test_control_resize_preflights_existing_interactive_binding(
    tmp_path: Path,
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    remove_ai_setting(tmp_path, "lane_execution")
    calls: list[list[str]] = []
    runtime = Runtime(
        runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
    )
    store = Store(tmp_path)
    try:
        ack = apply_control(
            store,
            runtime,
            {
                "type": "control",
                "session_id": SESSION_A,
                "action": "resize",
                "payload": {"cols": 100, "rows": 40},
            },
        )
    finally:
        store.close()

    assert ack["ok"] is False
    assert "lane_execution is not configured" in ack["error"]
    assert calls == []


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_session_input_preflights_before_interactive_runtime_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    remove_ai_setting(tmp_path, missing)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "Runtime",
        lambda *a, **k: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )

    with pytest.raises(SystemExit, match=f"{missing} is not configured"):
        run(
            tmp_path,
            ["session", "input", "--id", SESSION_A, "--data", "continue"],
        )

    assert calls == []


def test_raw_session_input_still_does_not_require_ai_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "Runtime",
        lambda *a, **k: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )

    run(
        tmp_path,
        ["session", "input", "--id", SESSION_A, "--data", "raw terminal"],
    )

    assert calls == [
        [
            "tmux",
            "send-keys",
            "-t",
            "agent-sess-1",
            "-l",
            "--",
            "raw terminal",
        ]
    ]


def test_authorized_interactive_input_preserves_runtime_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    (tmp_path / "runtime-targets.json").write_text(
        json.dumps({SESSION_A: ["remote-runtime"]}), encoding="utf-8"
    )
    calls: list[list[str]] = []

    def runtime_factory(*_args: object, **kwargs: object) -> Runtime:
        targets = kwargs.get("tmux_targets")
        assert targets == {SESSION_A: ["remote-runtime"]}
        assert isinstance(targets, dict)
        return Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", ""),
            tmux_targets=targets,
        )

    monkeypatch.setattr(main_mod, "Runtime", runtime_factory)

    run(
        tmp_path,
        ["session", "input", "--id", SESSION_A, "--data", "authorized"],
    )

    assert calls == [
        [
            "remote-runtime",
            "tmux",
            "send-keys",
            "-t",
            "agent-sess-1",
            "-l",
            "--",
            "authorized",
        ]
    ]


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_keep_working_preflights_before_initial_runtime_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    remove_ai_setting(tmp_path, missing)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "Runtime",
        lambda *a, **k: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )

    with pytest.raises(SystemExit, match=f"{missing} is not configured"):
        run(
            tmp_path,
            ["session", "keep-working", "--id", SESSION_A, "--once"],
        )

    assert calls == []


def test_keep_working_rechecks_preflight_before_each_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, "Waiting for response… 1s [stop]", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    calls_before_configuration_removal: list[list[list[str]]] = []

    def remove_configuration(_seconds: float) -> None:
        calls_before_configuration_removal.append(list(calls))
        remove_ai_setting(tmp_path, "lane_execution")

    monkeypatch.setattr(main_mod.time, "sleep", remove_configuration)

    with pytest.raises(SystemExit, match="lane_execution is not configured"):
        run(
            tmp_path,
            ["session", "keep-working", "--id", SESSION_A, "--follow"],
        )

    assert len(calls_before_configuration_removal) == 1
    assert calls == calls_before_configuration_removal[0]


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_knock_once_preflights_before_interactive_runtime_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "mail-preflight",
            {
                "id": "mail-preflight",
                "session_id": "sender",
                "type": "message",
                "payload": {"to_session": SESSION_A, "body": "secret"},
                "execution_status": "pending",
            },
        )
        store.enqueue_wake("mail-preflight", SESSION_A)
    finally:
        store.close()
    remove_ai_setting(tmp_path, missing)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "Runtime",
        lambda *a, **k: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )

    with pytest.raises(SystemExit, match=f"{missing} is not configured"):
        run(tmp_path, ["knock", "--once"])

    assert calls == []


@pytest.mark.parametrize("starter", ["watch", "supervise"])
@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("models", "models is not configured"),
        ("lane_execution", "lane_execution is not configured"),
        ("binding", "interactive AI binding changed"),
    ],
)
def test_assignment_starters_preflight_before_attached_runtime_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    starter: str,
    mutation: str,
    error: str,
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "runner"])
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = {
            "control": "attached",
            "tmux_session": "agent-sess-1",
            "interactive_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "ai_binding": expected_ai_binding(),
        }
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    path = tmp_path / "ai-accounts.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "binding":
        config["roles"]["interactive-builder"]["model"] = "changed-after-attach"
    else:
        config.pop(mutation)
    path.write_text(json.dumps(config), encoding="utf-8")

    calls: list[list[str]] = []
    runtime = Runtime(
        runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
    )
    monkeypatch.setattr(main_mod, "_runtime", lambda: runtime)

    if starter == "watch":
        monkeypatch.setattr(main_mod, "scan_assigned", lambda *a, **k: ([], 0))
        monkeypatch.setattr(
            main_mod, "assigned_workspace_root", lambda _store: tmp_path
        )
        monkeypatch.setattr(main_mod, "assigned_session_id", lambda _home: SESSION_A)
        monkeypatch.setattr(
            main_mod,
            "pending_assigned",
            lambda _store, _sid: [{"id": "assignment"}],
        )

        def dispatch(
            _store: object,
            _activity_id: str,
            *,
            pane_up: Callable[[str], bool] | None = None,
            **_kwargs: object,
        ) -> str:
            assert pane_up is not None
            return "held" if pane_up(SESSION_A) else "started"

        monkeypatch.setattr(main_mod, "dispatch_assigned", dispatch)
        argv = ["watch", "assigned"]
    else:
        argv = ["supervise", "--session", SESSION_A, "--once"]

    with pytest.raises(SystemExit, match=error):
        main_mod.main(argv)
    assert calls == []


def test_supervise_preflights_fresh_interactive_session_before_runtime_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "runner"])
    remove_ai_setting(tmp_path, "models")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod,
        "_runtime",
        lambda: Runtime(
            runner=lambda argv: calls.append(list(argv)) or Completed(0, "", "")
        ),
    )

    with pytest.raises(SystemExit, match="models is not configured"):
        main_mod.cmd_supervise(["--session", SESSION_A, "--once"])

    assert calls == []


def test_supervise_raw_terminal_still_does_not_require_ai_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_cli.telegram_act as telegram_mod

    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    # No ai-accounts.json: drivers.json only names the pane_driver that reads
    # the raw pane.
    write_drivers(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "runner"])
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = {
            "control": "attached",
            "tmux_session": f"agent-{SESSION_A}",
        }
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, "raw terminal", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "_runtime", lambda: Runtime(runner=runner))
    monkeypatch.setattr(telegram_mod, "notify_status", lambda *a, **k: None)

    main_mod.cmd_supervise(["--session", SESSION_A, "--once"])

    assert any(argv[:2] == ["tmux", "has-session"] for argv in calls)


@pytest.mark.parametrize("starter", ["watch", "supervise"])
def test_assignment_knocks_use_preflight_runtime_in_deliver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    starter: str,
) -> None:
    import agent_cli.knock as knock_mod
    import agent_cli.supervise as supervise_mod
    import agent_cli.telegram_act as telegram_mod

    sid = "assigned-session"
    store = SimpleNamespace(home=tmp_path, close=lambda: None)
    runtime = Runtime(
        runner=lambda _argv: Completed(0, "", "")
    )
    checked: list[str] = []
    delivered: list[str] = []

    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    # The assigned session has no interactive role; pane_driver reads its pane.
    write_drivers(tmp_path)
    monkeypatch.setattr(main_mod, "open_store", lambda: store)
    monkeypatch.setattr(main_mod, "_runtime", lambda: runtime)
    monkeypatch.setattr(main_mod, "_interactive_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(
        main_mod,
        "_interactive_runtime_preflight",
        lambda _store, session_id, **_kwargs: checked.append(session_id),
    )

    def deliver(
        _store: object,
        _runtime: object,
        activity_id: str,
    ) -> str:
        assert _runtime.exists(sid)
        delivered.append(activity_id)
        return "sent"

    monkeypatch.setattr(knock_mod, "deliver", deliver)
    if starter == "watch":
        monkeypatch.setattr(main_mod, "scan_assigned", lambda *a, **k: ([], 0))
        monkeypatch.setattr(
            main_mod, "assigned_workspace_root", lambda _store: tmp_path
        )
        monkeypatch.setattr(main_mod, "assigned_session_id", lambda _home: sid)
        monkeypatch.setattr(
            main_mod,
            "pending_assigned",
            lambda _store, _sid: [{"id": "assignment"}],
        )

        def dispatch(
            _store: object,
            activity_id: str,
            *,
            knock: Callable[[str], str],
            **_kwargs: object,
        ) -> str:
            return knock(activity_id)

        monkeypatch.setattr(main_mod, "dispatch_assigned", dispatch)
        main_mod.cmd_watch(["assigned"])
    else:
        monkeypatch.setattr(telegram_mod, "reset_idle_clock", lambda *a, **k: None)
        monkeypatch.setattr(telegram_mod, "notify_status", lambda *a, **k: None)

        def supervise_tick(
            *_args: object,
            knock: Callable[[str], str],
            **_kwargs: object,
        ) -> str:
            knock("assignment")
            return "supervise commission"

        monkeypatch.setattr(supervise_mod, "tick", supervise_tick)
        main_mod.cmd_supervise(["--session", sid, "--once"])

    assert delivered == ["assignment"]
    assert checked
    assert set(checked) == {sid}


def test_assignment_starters_forward_the_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "assigned-session"
    store = SimpleNamespace(home=tmp_path, close=lambda: None)
    runtime = Runtime(runner=lambda _argv: Completed(1, "", ""))
    starts: list[tuple[str, str | None]] = []
    # The assigned session has no interactive role; pane_driver reads its pane.
    write_drivers(tmp_path)

    def session_start(
        _store: object,
        _runtime: object,
        session_id: str,
        _command: str | None,
        _cols: int | None,
        _rows: int | None,
        **kwargs: object,
    ) -> str:
        cwd = kwargs.get("cwd")
        assert cwd is None or isinstance(cwd, str)
        starts.append((session_id, cwd))
        return "tmux-name"

    monkeypatch.setenv("AGENT_HOME", str(tmp_path))
    monkeypatch.setattr(main_mod, "open_store", lambda: store)
    monkeypatch.setattr(main_mod, "_runtime", lambda: runtime)
    monkeypatch.setattr(main_mod, "_session_start", session_start)
    monkeypatch.setattr(
        main_mod, "_interactive_runtime_preflight", lambda *a, **k: None
    )
    monkeypatch.setattr(main_mod, "scan_assigned", lambda *a, **k: ([], 0))
    monkeypatch.setattr(main_mod, "assigned_workspace_root", lambda _store: tmp_path / "watch")
    monkeypatch.setattr(main_mod, "assigned_session_id", lambda _home: sid)
    monkeypatch.setattr(main_mod, "pending_assigned", lambda _store, _sid: [{"id": "assignment"}])

    def dispatch(
        _store: object,
        _activity_id: str,
        *,
        start: Callable[[str, Path], None],
        workspace_root: Path,
        **_kwargs: object,
    ) -> str:
        start(sid, workspace_root / sid)
        return "started"

    monkeypatch.setattr(main_mod, "dispatch_assigned", dispatch)
    main_mod.cmd_watch(["assigned"])

    import agent_cli.supervise as supervise_mod
    import agent_cli.telegram_act as telegram_mod

    supervise_cwd = tmp_path / "supervise" / sid

    def tick(
        *_args: object,
        start: Callable[[str, Path], None],
        **_kwargs: object,
    ) -> str:
        start(sid, supervise_cwd)
        return "supervise commission"

    monkeypatch.setattr(supervise_mod, "tick", tick)
    monkeypatch.setattr(telegram_mod, "reset_idle_clock", lambda *a, **k: None)
    monkeypatch.setattr(telegram_mod, "notify_status", lambda *a, **k: None)
    main_mod.cmd_supervise(["--session", sid, "--once"])

    assert starts == [
        (sid, str(tmp_path / "watch" / sid)),
        (sid, str(supervise_cwd)),
    ]


def test_remote_interactive_mode_is_rejected_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    path = tmp_path / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["lane_execution"] = {"mode": "remote", "host": "lane.example"}
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])

    with pytest.raises(SystemExit, match="remote.*not supported"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)


def test_two_sessions_use_different_interactive_accounts_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(1, "", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    run(tmp_path, ["session", "register", "--id", SESSION_B, "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    run(tmp_path, ["session", "start", "--id", SESSION_B, "--provider", "driver-a"])
    news = [c for c in calls if c[:2] == ["tmux", "new-session"] and "tool-a" in c]
    assert len(news) == 2
    argv_a, argv_b = news
    assert OPERATOR_INTERACTIVE_MODEL in argv_a
    assert OPERATOR_INTERACTIVE_MODEL_B in argv_b
    assert f"TOOL_A_HOME={OPERATOR_HOME_A}" in argv_a
    assert f"TOOL_A_HOME={OPERATOR_HOME_B}" in argv_b
    assert OPERATOR_INTERACTIVE_MODEL_B not in argv_a
    assert OPERATOR_INTERACTIVE_MODEL not in argv_b
    store = Store(tmp_path)
    try:
        assert store.row("session", SESSION_A)["runtime"]["ai_binding"] == expected_ai_binding()
        assert store.row("session", SESSION_B)["runtime"]["ai_binding"] == expected_ai_binding(
            role="interactive-builder-b",
            account="interactive-account-b",
            model=OPERATOR_INTERACTIVE_MODEL_B,
            config_dir=OPERATOR_HOME_B,
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutate",
    [
        "model",
        "config_dir",
        "account",
        "role",
    ],
)
def test_resume_refuses_account_config_role_or_model_change_before_process_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: str
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            if any(c[:2] == ["tmux", "new-session"] for c in calls[:-1]) and not any(
                c[:2] == ["tmux", "kill-session"] for c in calls[:-1]
            ):
                return Completed(0, "", "")
            return Completed(1, "", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    run(tmp_path, ["session", "stop", "--id", SESSION_A])
    before = len([c for c in calls if c[:2] == ["tmux", "new-session"]])

    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    if mutate == "model":
        raw["roles"]["interactive-builder"]["model"] = "changed-after-first-start"
    elif mutate == "config_dir":
        raw["accounts"]["interactive-account-a"]["config_dir"] = "/operator/path/interactive-changed"
    elif mutate == "account":
        raw["roles"]["interactive-builder"]["account"] = "interactive-account-b"
    else:
        raw["roles"]["interactive-builder-alt"] = {
            "account": "interactive-account-a",
            "model": OPERATOR_INTERACTIVE_MODEL,
            "access": "workspace-write",
        }
        raw["sessions"][SESSION_A]["interactive"] = "interactive-builder-alt"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SystemExit, match="interactive AI binding changed"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    after = len([c for c in calls if c[:2] == ["tmux", "new-session"]])
    assert after == before


def test_resume_accepts_legacy_binding_with_lane_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    bind_interactive_runtime(tmp_path, SESSION_A)
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"]["ai_binding"]["lane_execution"] = {
            "mode": "this-device-is-lane-host"
        }
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()

    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
    finally:
        store.close()


def test_legacy_interactive_session_without_ai_binding_refuses_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy sessions with a session id but no ai_binding must not silently resume."""
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(1, "", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = {
            "interactive_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "provider": "driver-a",
            "model": "legacy-interactive-model",
            "tmux_session": "agent-sess-1",
            "control": "stopped",
        }
        store.write(
            "session",
            "update",
            SESSION_A,
            {k: v for k, v in row.items() if not str(k).startswith("_")},
        )
    finally:
        store.close()

    with pytest.raises(SystemExit, match="interactive AI binding changed"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)


def test_cli_model_flag_must_match_configured_interactive_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(tmp_path)
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(1, "", "session not found")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    # Old model-switch-on-resume behavior is gone: mismatched --model is refused.
    with pytest.raises(SystemExit, match="--model does not match"):
        run(
            tmp_path,
            [
                "session",
                "start",
                "--id",
                SESSION_A,
                "--provider",
                "driver-a",
                "--model",
                "some-other-model",
            ],
        )
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)

    run(
        tmp_path,
        [
            "session",
            "start",
            "--id",
            SESSION_A,
            "--provider",
            "driver-a",
            "--model",
            OPERATOR_INTERACTIVE_MODEL,
        ],
    )
    assert any(c[:2] == ["tmux", "new-session"] for c in calls)


def test_cli_interactive_role_of_another_driver_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(
        tmp_path,
        sessions={SESSION_A: {"interactive": "interactive-driver-b-role", "lanes": {}}},
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    with pytest.raises(SystemExit, match="does not match the requested provider"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)


# --- placeholder interactive drivers ------------------------------------------

LEGACY_ID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


def write_placeholder_interactive(home: Path, *, role: str = "driver-a-writer") -> None:
    write_drivers(home)
    data = accounts_document()
    data["sessions"] = {SESSION_A: {"interactive": role}}
    write_accounts(home, data)


def placeholder_binding(role: str = "driver-a-writer") -> dict:
    driver = role.rsplit("-", 1)[0]
    return {
        "role": role,
        "account": f"{driver}-account",
        "provider": driver,
        "model": f"{driver}-model",
        "access": "read-only" if role.endswith("-reader") else "workspace-write",
        "configuration": hashlib.sha256(CONFIG_DIRS[driver].encode()).hexdigest(),
    }


def _tmux_runner(calls: list[list[str]]) -> Callable[[list[str]], Completed]:
    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "has-session"]:
            created = [c for c in calls[:-1] if c[:2] == ["tmux", "new-session"]]
            killed = [c for c in calls[:-1] if c[:2] == ["tmux", "kill-session"]]
            return Completed(0 if len(created) > len(killed) else 1, "", "")
        return Completed(0, "", "")

    return runner


def _pane_command(calls: list[list[str]]) -> list[str]:
    created = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
    return created[created.index("--") + 1 :]


def _session_runtime(home: Path) -> dict:
    store = Store(home)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        return dict(row["runtime"])
    finally:
        store.close()


@pytest.mark.parametrize(
    ("role", "access_args"),
    [("driver-a-writer", []), ("driver-a-reader", ["--deny", "Edit"])],
)
def test_interactive_start_builds_the_pane_from_driver_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    role: str,
    access_args: list[str],
) -> None:
    write_placeholder_interactive(tmp_path, role=role)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod, "Runtime", lambda *a, **k: Runtime(runner=_tmux_runner(calls))
    )
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    runtime = _session_runtime(tmp_path)
    session = runtime["interactive_session_id"]
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", session)
    assert f"interactive={session}" in capsys.readouterr().out
    assert runtime["provider"] == "driver-a"
    assert runtime["ai_binding"] == placeholder_binding(role)
    assert "interactive_start_pending" not in runtime
    drivers = driver_specs()
    role_prefix = ["env", *[p for key in role_clear_env(drivers) for p in ("-u", key)]]
    scrub_prefix = ["env", *[p for key in lane_scrub_env(drivers) for p in ("-u", key)]]
    tail = [*access_args, "--deny", "Shell"]
    assert _pane_command(calls) == [
        *role_prefix,
        "TOOL_A_HOME=/operator/path/tool-a",
        *scrub_prefix,
        "tool-a", "--session-id", session, "--model", "driver-a-model",
        *tail,
    ]

    run(tmp_path, ["session", "stop", "--id", SESSION_A])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])
    resumed = _pane_command(calls)
    assert resumed[resumed.index("tool-a") :] == [
        "tool-a", "--resume", session, "--model", "driver-a-model", *tail,
    ]
    assert _session_runtime(tmp_path)["interactive_session_id"] == session


def test_interactive_start_accepts_delegate_without_prefixing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_placeholder_interactive(tmp_path)
    path = tmp_path / "ai-accounts.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["lane_execution"] = {
        "mode": "delegate",
        "argv": ["/opt/operator/run"],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod, "Runtime", lambda *a, **k: Runtime(runner=_tmux_runner(calls))
    )
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "driver-a"])

    command = _pane_command(calls)
    assert "/opt/operator/run" not in command
    assert "--workdir" not in command
    assert command[command.index("tool-a") :][:2] == ["tool-a", "--session-id"]
    assert "interactive_start_pending" not in _session_runtime(tmp_path)


def test_control_start_interactive_starts_the_driver_of_the_role(tmp_path: Path) -> None:
    write_placeholder_interactive(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    calls: list[list[str]] = []
    store = Store(tmp_path)
    try:
        ack = apply_control(
            store,
            Runtime(runner=_tmux_runner(calls)),
            {
                "type": "control",
                "session_id": SESSION_A,
                "action": "start",
                "payload": {"interactive": True, "cols": 80, "rows": 24},
            },
        )
    finally:
        store.close()
    assert ack["ok"] is True, ack
    runtime = _session_runtime(tmp_path)
    assert runtime["provider"] == "driver-a"
    assert runtime["ai_binding"] == placeholder_binding()
    command = _pane_command(calls)
    assert command[command.index("tool-a") :][:3] == [
        "tool-a", "--session-id", runtime["interactive_session_id"],
    ]


@pytest.mark.parametrize(
    ("role", "payload", "error"),
    [
        (
            "driver-c-writer",
            {"interactive": True},
            "provider must be driver-a (see drivers.json, docs/ai-accounts.md#drivers)",
        ),
        (
            "driver-c-writer",
            {"provider": "driver-c"},
            "provider must be driver-a (see drivers.json, docs/ai-accounts.md#drivers)",
        ),
        (
            "driver-c-writer",
            {"provider": "driver-a"},
            "configured interactive role does not match the requested provider",
        ),
        (
            "driver-a-writer",
            {"provider": "driver-x"},
            "provider must be driver-a (see drivers.json, docs/ai-accounts.md#drivers)",
        ),
        ("driver-a-writer", {"interactive": "yes"}, "interactive must be a boolean"),
        ("driver-a-writer", {"model": "driver-a-model"}, "--model requires --provider"),
    ],
)
def test_interactive_start_refusals_start_no_process(
    tmp_path: Path, role: str, payload: dict, error: str
) -> None:
    write_placeholder_interactive(tmp_path, role=role)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    calls: list[list[str]] = []
    store = Store(tmp_path)
    try:
        ack = apply_control(
            store,
            Runtime(runner=_tmux_runner(calls)),
            {"type": "control", "session_id": SESSION_A, "action": "start", "payload": payload},
        )
    finally:
        store.close()
    assert ack["ok"] is False
    assert ack["error"] == error
    assert calls == []


@pytest.mark.parametrize("provider", [None, "driver-a"])
def test_runtime_keys_named_after_the_driver_read_as_neutral_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str | None,
) -> None:
    write_placeholder_interactive(tmp_path)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        row["runtime"] = {
            "control": "stopped",
            "tmux_session": f"agent-{SESSION_A}",
            "provider": "driver-a",
            "model": "driver-a-model",
            "driver-a_session_id": LEGACY_ID,
            "ai_binding": placeholder_binding(),
        }
        store.write(
            "session",
            "update",
            SESSION_A,
            {key: value for key, value in row.items() if not key.startswith("_")},
        )
    finally:
        store.close()
    calls: list[list[str]] = []
    monkeypatch.setattr(
        main_mod, "Runtime", lambda *a, **k: Runtime(runner=_tmux_runner(calls))
    )
    argv = ["session", "start", "--id", SESSION_A]
    if provider is not None:
        argv.extend(["--provider", provider])
    capsys.readouterr()
    run(tmp_path, argv)

    assert f"interactive={LEGACY_ID}" in capsys.readouterr().out
    runtime = _session_runtime(tmp_path)
    assert runtime["interactive_session_id"] == LEGACY_ID
    assert "driver-a_session_id" not in runtime
    if provider is not None:
        command = _pane_command(calls)
        assert command[command.index("tool-a") :][:3] == ["tool-a", "--resume", LEGACY_ID]


def test_allow_reads_the_session_from_the_configured_session_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = "sess-from-driver"
    not_configured = re.escape(DRIVERS_NOT_CONFIGURED)
    monkeypatch.setenv("TOOL_A_SESSION_ID", session)
    run(tmp_path, ["init"])
    run(
        tmp_path,
        ["session", "register", "--id", session, "--kind", "human", "--skill", "spine"],
    )

    # Without drivers.json the session variable is unknown: fail, never allow.
    with pytest.raises(SystemExit, match=not_configured):
        main_mod._driver_session_env(tmp_path)
    for action in ("claim-done", "pr-ready", "pr-create", "task-done"):
        with pytest.raises(SystemExit, match=not_configured):
            run(tmp_path, ["allow", "--action", action])
    capsys.readouterr()
    run(tmp_path, ["allow", "--action", "claim-done", "--session", session])
    assert f"allow action=claim-done session={session}" in capsys.readouterr().out

    write_drivers(tmp_path)
    write_accounts(
        tmp_path,
        {
            "models": {"first": "driver-a", "second": "driver-b"},
            "lane_execution": {"mode": "this-device-is-lane-host"},
        },
    )
    assert main_mod._driver_session_env(tmp_path) == session
    run(
        tmp_path,
        ["task", "create", "--session", session, "--workflow", "implement", "--title", "Ship"],
    )
    out = capsys.readouterr().out
    tid = [line for line in out.splitlines() if line.startswith("task ")][-1].split()[1]
    with pytest.raises(SystemExit) as denied:
        run(tmp_path, ["allow", "--action", "claim-done"])
    assert denied.value.code == 2
    out = capsys.readouterr().out
    assert "deny action=claim-done reason=open tasks are not ready_for_done" in out

    # A variable exported empty counts as set: the empty session id selects no
    # task, so task-done does not fall back to the task's own session.
    monkeypatch.setenv("TOOL_A_SESSION_ID", "")
    assert main_mod._driver_session_env(tmp_path) == ""
    with pytest.raises(SystemExit) as empty:
        run(tmp_path, ["allow", "--action", "task-done", "--task", tid])
    assert empty.value.code == 2
    assert f"reason=task {tid} not found in this session" in capsys.readouterr().out
    run(tmp_path, ["allow", "--action", "pr-create", "--draft", "true", "--json"])
    assert json.loads(capsys.readouterr().out)["session"] == ""
    monkeypatch.delenv("TOOL_A_SESSION_ID")
    assert main_mod._driver_session_env(tmp_path) is None
    monkeypatch.setenv("TOOL_A_SESSION_ID", "sess-unknown")
    with pytest.raises(SystemExit, match="unknown session: sess-unknown"):
        run(tmp_path, ["allow", "--action", "pr-ready"])

    # A drivers.json in which no driver configures interactive.session_env
    # cannot name the variable either: fail, never allow without the session,
    # even while the driver exports it.
    monkeypatch.setenv("TOOL_A_SESSION_ID", session)
    no_session_env = re.escape(SESSION_ENV_NOT_CONFIGURED)
    without = driver_document()
    del without["drivers"]["driver-a"]["interactive"]["session_env"]
    for document in (without, {}):
        write_drivers(tmp_path, document)
        with pytest.raises(SystemExit, match=no_session_env):
            main_mod._driver_session_env(tmp_path)
        for action in ("claim-done", "pr-ready", "pr-create", "task-done"):
            with pytest.raises(SystemExit, match=no_session_env):
                run(tmp_path, ["allow", "--action", action])
    write_drivers(tmp_path, without)
    capsys.readouterr()
    with pytest.raises(SystemExit) as explicit:
        run(tmp_path, ["allow", "--action", "claim-done", "--session", session])
    assert explicit.value.code == 2
    out = capsys.readouterr().out
    assert "deny action=claim-done reason=open tasks are not ready_for_done" in out
