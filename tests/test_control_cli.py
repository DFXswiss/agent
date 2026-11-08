from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import main as main_mod
from agent_cli.main import apply_control, main, should_sync_on_ws
from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store

OPERATOR_INTERACTIVE_MODEL = "operator-interactive-model"
OPERATOR_INTERACTIVE_MODEL_B = "operator-interactive-model-b"
OPERATOR_GROK_HOME = "/operator/path/interactive-grok-a"
OPERATOR_GROK_HOME_B = "/operator/path/interactive-grok-b"
SESSION_A = "sess-1"
SESSION_B = "sess-2"


def write_operator_interactive_ai_accounts(
    home: Path,
    *,
    sessions: dict | None = None,
    model: str = OPERATOR_INTERACTIVE_MODEL,
    config_dir: str = OPERATOR_GROK_HOME,
    account_name: str = "interactive-grok",
    role_name: str = "interactive-builder",
    access: str = "workspace-write",
) -> None:
    """Write operator-supplied ai-accounts.json for positive interactive starts.

    Not installed by default. Call explicitly for positive grok provider cases;
    raw-shell starts (provider=None) must not rely on this helper.
    """
    data = {
        "lane_execution": {"mode": "this-device-is-lane-host"},
        "models": {"first": "grok", "second": "codex"},
        "accounts": {
            account_name: {"provider": "grok", "config_dir": config_dir},
            "interactive-grok-b": {
                "provider": "grok",
                "config_dir": OPERATOR_GROK_HOME_B,
            },
            "interactive-codex": {
                "provider": "codex",
                "config_dir": "/operator/path/interactive-codex",
            },
        },
        "roles": {
            role_name: {
                "account": account_name,
                "model": model,
                "access": access,
            },
            "interactive-builder-b": {
                "account": "interactive-grok-b",
                "model": OPERATOR_INTERACTIVE_MODEL_B,
                "access": "workspace-write",
            },
            "interactive-codex-role": {
                "account": "interactive-codex",
                "model": "operator-codex-model",
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
    (home / "ai-accounts.json").write_text(json.dumps(data), encoding="utf-8")


def expected_ai_binding(
    *,
    role: str = "interactive-builder",
    account: str = "interactive-grok",
    model: str = OPERATOR_INTERACTIVE_MODEL,
    config_dir: str = OPERATOR_GROK_HOME,
    access: str = "workspace-write",
) -> dict:
    return {
        "role": role,
        "account": account,
        "provider": "grok",
        "model": model,
        "access": access,
        "configuration": hashlib.sha256(config_dir.encode()).hexdigest(),
        "lane_execution": {"mode": "this-device-is-lane-host"},
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
            "provider": "grok",
            "model": OPERATOR_INTERACTIVE_MODEL,
            "grok_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
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


def test_cli_start_provider_grok_mints_uuid_and_resumes(
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
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    out = capsys.readouterr().out
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        gid = row["runtime"]["grok_session_id"]
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", gid)
        assert f"grok={gid}" in out
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
        assert row["runtime"]["model"] == OPERATOR_INTERACTIVE_MODEL
        first = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
        assert "env" in first
        assert "ANTHROPIC_API_KEY" in first
        assert "XAI_API_KEY" in first
        assert f"GROK_HOME={OPERATOR_GROK_HOME}" in first
        assert "--session-id" in first
        assert gid in first
        assert "--model" in first and OPERATOR_INTERACTIVE_MODEL in first
        assert "grok-4.6" not in first
        assert first[first.index("--session-id") + 1] != SESSION_A
        # Nested role env_prefix precedes the existing grok_tmux_command_argv env strip.
        assert first[first.index("--") + 1] == "env"
        second_env = first.index("env", first.index("--") + 2)
        assert second_env > first.index(f"GROK_HOME={OPERATOR_GROK_HOME}")
    finally:
        store.close()

    run(tmp_path, ["session", "stop", "--id", SESSION_A])
    calls.clear()
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    resume = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
    assert "--resume" in resume
    assert "--session-id" not in resume
    assert OPERATOR_INTERACTIVE_MODEL in resume
    assert f"GROK_HOME={OPERATOR_GROK_HOME}" in resume
    store = Store(tmp_path)
    try:
        row = store.row("session", SESSION_A)
        assert row is not None
        assert resume[resume.index("--resume") + 1] == row["runtime"]["grok_session_id"]
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
    finally:
        store.close()


def test_cli_start_grok_replaces_bare_tmux(
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
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    assert any(c[:2] == ["tmux", "kill-session"] for c in calls)
    grok_news = [c for c in calls if c[:2] == ["tmux", "new-session"] and "grok" in c]
    assert len(grok_news) == 1
    assert "--session-id" in grok_news[0]
    assert OPERATOR_INTERACTIVE_MODEL in grok_news[0]
    store = Store(tmp_path)
    try:
        gid = store.row("session", SESSION_A)["runtime"]["grok_session_id"]
        assert gid in grok_news[0]
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
            ["session", "start", "--id", SESSION_A, "--provider", "grok", "--cmd", "bash"],
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
        assert "grok_session_id" not in rt
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
                "payload": {"provider": "grok", "cols": 80, "rows": 24},
            },
        )
        assert ack["ok"] is True
        row = store.row("session", "s1")
        assert row is not None
        assert row["runtime"]["provider"] == "grok"
        assert row["runtime"]["model"] == OPERATOR_INTERACTIVE_MODEL
        assert row["runtime"]["ai_binding"] == expected_ai_binding()
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            row["runtime"]["grok_session_id"],
        )
        grok_new = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
        assert "--session-id" in grok_new
        assert OPERATOR_INTERACTIVE_MODEL in grok_new
        assert f"GROK_HOME={OPERATOR_GROK_HOME}" in grok_new
        assert "grok-4.6" not in grok_new

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
                "payload": {"provider": "grok", "command": "bash"},
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
    """Genuinely empty AI config: provider=grok must not start tmux/grok."""
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        return Completed(0, "", "")

    monkeypatch.setattr(main_mod, "Runtime", lambda *a, **k: Runtime(runner=runner))
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", SESSION_A, "--kind", "human"])
    with pytest.raises(SystemExit, match="No AI session configured"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
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
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
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
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
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
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])

    assert calls == []


@pytest.mark.parametrize("missing", ["models", "lane_execution"])
def test_session_input_preflights_before_grok_runtime_input(
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


def test_authorized_grok_input_preserves_runtime_target(
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
def test_knock_once_preflights_before_grok_runtime_probe(
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
            "grok_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
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
        monkeypatch.setattr(main_mod, "assigned_workspace_root", lambda _store: tmp_path)
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


def test_assignment_starters_forward_the_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "assigned-session"
    store = SimpleNamespace(home=tmp_path, close=lambda: None)
    runtime = SimpleNamespace(exists=lambda _sid: False)
    starts: list[tuple[str, str | None]] = []

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
    monkeypatch.setattr(main_mod, "_interactive_preflight", lambda *a, **k: None)
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
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
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
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    run(tmp_path, ["session", "start", "--id", SESSION_B, "--provider", "grok"])
    news = [c for c in calls if c[:2] == ["tmux", "new-session"] and "grok" in c]
    assert len(news) == 2
    argv_a, argv_b = news
    assert OPERATOR_INTERACTIVE_MODEL in argv_a
    assert OPERATOR_INTERACTIVE_MODEL_B in argv_b
    assert f"GROK_HOME={OPERATOR_GROK_HOME}" in argv_a
    assert f"GROK_HOME={OPERATOR_GROK_HOME_B}" in argv_b
    assert OPERATOR_INTERACTIVE_MODEL_B not in argv_a
    assert OPERATOR_INTERACTIVE_MODEL not in argv_b
    store = Store(tmp_path)
    try:
        assert store.row("session", SESSION_A)["runtime"]["ai_binding"] == expected_ai_binding()
        assert store.row("session", SESSION_B)["runtime"]["ai_binding"] == expected_ai_binding(
            role="interactive-builder-b",
            account="interactive-grok-b",
            model=OPERATOR_INTERACTIVE_MODEL_B,
            config_dir=OPERATOR_GROK_HOME_B,
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
    run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    run(tmp_path, ["session", "stop", "--id", SESSION_A])
    before = len([c for c in calls if c[:2] == ["tmux", "new-session"]])

    raw = json.loads((tmp_path / "ai-accounts.json").read_text(encoding="utf-8"))
    if mutate == "model":
        raw["roles"]["interactive-builder"]["model"] = "changed-after-first-start"
    elif mutate == "config_dir":
        raw["accounts"]["interactive-grok"]["config_dir"] = "/operator/path/interactive-changed"
    elif mutate == "account":
        raw["roles"]["interactive-builder"]["account"] = "interactive-grok-b"
    else:
        raw["roles"]["interactive-builder-alt"] = {
            "account": "interactive-grok",
            "model": OPERATOR_INTERACTIVE_MODEL,
            "access": "workspace-write",
        }
        raw["sessions"][SESSION_A]["interactive"] = "interactive-builder-alt"
    (tmp_path / "ai-accounts.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SystemExit, match="interactive AI binding changed"):
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    after = len([c for c in calls if c[:2] == ["tmux", "new-session"]])
    assert after == before


def test_legacy_grok_session_without_ai_binding_refuses_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy sessions with grok_session_id but no ai_binding must not silently resume."""
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
            "grok_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "provider": "grok",
            "model": "grok-4.6",
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
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
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
                "grok",
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
            "grok",
            "--model",
            OPERATOR_INTERACTIVE_MODEL,
        ],
    )
    assert any(c[:2] == ["tmux", "new-session"] for c in calls)


def test_cli_interactive_codex_role_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_operator_interactive_ai_accounts(
        tmp_path,
        sessions={SESSION_A: {"interactive": "interactive-codex-role", "lanes": {}}},
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
        run(tmp_path, ["session", "start", "--id", SESSION_A, "--provider", "grok"])
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)
