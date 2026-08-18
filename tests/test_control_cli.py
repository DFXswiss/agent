from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_cli import main as main_mod
from agent_cli.main import apply_control, main, should_sync_on_ws
from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store


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
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", "sess-1", "--provider", "grok"])
    out = capsys.readouterr().out
    store = Store(tmp_path / "ledger.sqlite")
    try:
        row = store.row("session", "sess-1")
        assert row is not None
        gid = row["runtime"]["grok_session_id"]
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", gid)
        assert f"grok={gid}" in out
        first = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
        assert "env" in first
        assert "ANTHROPIC_API_KEY" in first
        assert "--session-id" in first
        assert gid in first
        assert "--model" in first and "grok-4.6" in first
        assert first[first.index("--session-id") + 1] != "sess-1"
    finally:
        store.close()

    run(tmp_path, ["session", "stop", "--id", "sess-1"])
    calls.clear()
    run(tmp_path, ["session", "start", "--id", "sess-1", "--provider", "grok"])
    resume = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
    assert "--resume" in resume
    assert "--session-id" not in resume
    store = Store(tmp_path / "ledger.sqlite")
    try:
        row = store.row("session", "sess-1")
        assert row is not None
        assert resume[resume.index("--resume") + 1] == row["runtime"]["grok_session_id"]
    finally:
        store.close()


def test_cli_start_grok_replaces_bare_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", "sess-1"])
    run(tmp_path, ["session", "start", "--id", "sess-1", "--provider", "grok"])
    assert any(c[:2] == ["tmux", "kill-session"] for c in calls)
    grok_news = [c for c in calls if c[:2] == ["tmux", "new-session"] and "grok" in c]
    assert len(grok_news) == 1
    assert "--session-id" in grok_news[0]
    store = Store(tmp_path / "ledger.sqlite")
    try:
        gid = store.row("session", "sess-1")["runtime"]["grok_session_id"]
        assert gid in grok_news[0]
    finally:
        store.close()


def test_cli_provider_and_cmd_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _ = _fake_runtime_factory()
    monkeypatch.setattr(main_mod, "Runtime", factory)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    with pytest.raises(SystemExit, match="cannot be used together"):
        run(tmp_path, ["session", "start", "--id", "sess-1", "--provider", "grok", "--cmd", "bash"])


def test_cli_start_owned_writes_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _ = _fake_runtime_factory()
    monkeypatch.setattr(main_mod, "Runtime", factory)
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "sess-1", "--kind", "human"])
    run(tmp_path, ["session", "start", "--id", "sess-1", "--cols", "80", "--rows", "24"])
    store = Store(tmp_path / "ledger.sqlite")
    try:
        row = store.row("session", "sess-1")
        assert row is not None
        rt = row.get("runtime")
        assert isinstance(rt, dict)
        assert rt["control"] == "attached"
        assert rt["tmux_session"] == "agent-sess-1"
        assert rt["cols"] == 80
        assert rt["rows"] == 24
    finally:
        store.close()


def test_cli_start_foreign_dies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, _ = _fake_runtime_factory()
    monkeypatch.setattr(main_mod, "Runtime", factory)
    run(tmp_path, ["init"])
    store = Store(tmp_path / "ledger.sqlite")
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
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s1", "--kind", "human"])
    store = Store(tmp_path / "ledger.sqlite")
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
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            row["runtime"]["grok_session_id"],
        )
        grok_new = [c for c in calls if c[:2] == ["tmux", "new-session"]][-1]
        assert "--session-id" in grok_new
        assert "grok-4.6" in grok_new

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
    run(tmp_path, ["init"])
    run(tmp_path, ["session", "register", "--id", "s1", "--kind", "human"])
    store = Store(tmp_path / "ledger.sqlite")
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
    store = Store(tmp_path / "ledger.sqlite")
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
    store = Store(tmp_path / "ledger.sqlite")
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
