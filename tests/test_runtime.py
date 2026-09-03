from __future__ import annotations

import re
import subprocess

import pytest

from agent_cli.runtime import (
    Completed,
    Runtime,
    grok_launch_argv,
    grok_model,
    grok_new_session_id,
    grok_tmux_command_argv,
    tmux_name,
)
from agent_cli.store import StoreError


def test_tmux_name_sanitizes() -> None:
    assert tmux_name("sess-1") == "agent-sess-1"
    assert tmux_name("a/b c") == "agent-a-b-c"
    assert tmux_name("x" * 60) == "agent-" + ("x" * 50)
    assert tmux_name("@@@") == "agent----"
    with pytest.raises(StoreError, match="empty tmux name"):
        tmux_name("")


def test_start_stop_input_argv() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            # first start: missing; after create: present for stop path
            if any(c[:2] == ["tmux", "new-session"] for c in calls[:-1]):
                return Completed(0, "", "")
            return Completed(1, "", "no server")
        return Completed(0, "", "")

    rt = Runtime(runner=runner)
    rt.start("sess-1", None, None, None)
    assert ["tmux", "new-session", "-d", "-s", "agent-sess-1"] in calls

    calls.clear()
    rt = Runtime(runner=runner)
    rt.start("sess-1", "bash -l", 80, 24)
    assert [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "agent-sess-1",
        "-x",
        "80",
        "-y",
        "24",
        "--",
        "bash",
        "-l",
    ] in calls

    calls.clear()

    def runner_exists(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        return Completed(0, "", "")

    rt = Runtime(runner=runner_exists)
    rt.start("sess-1", None, 100, 40)
    assert ["tmux", "new-session", "-d", "-s", "agent-sess-1"] not in calls
    assert ["tmux", "resize-window", "-t", "agent-sess-1", "-x", "100", "-y", "40"] in calls

    calls.clear()

    def runner_stop(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        return Completed(0, "", "")

    rt = Runtime(runner=runner_stop)
    rt.stop("sess-1")
    assert ["tmux", "kill-session", "-t", "agent-sess-1"] in calls

    calls.clear()
    rt = Runtime(runner=lambda argv: (calls.append(list(argv)) or Completed(0, "", "")))
    rt.input_text("sess-1", "hello")
    assert ["tmux", "send-keys", "-t", "agent-sess-1", "-l", "--", "hello"] in calls

    calls.clear()
    rt = Runtime(runner=lambda argv: (calls.append(list(argv)) or Completed(0, "", "")))
    rt.input_key("sess-1", "enter")
    assert ["tmux", "send-keys", "-t", "agent-sess-1", "Enter"] in calls
    calls.clear()
    rt.input_key("sess-1", "ctrl-c")
    assert ["tmux", "send-keys", "-t", "agent-sess-1", "C-c"] in calls
    calls.clear()
    rt.input_key("sess-1", "tab")
    assert ["tmux", "send-keys", "-t", "agent-sess-1", "Tab"] in calls

    with pytest.raises(SystemExit, match="unknown key"):
        rt.input_key("sess-1", "escape")


def test_grok_session_id_is_uuid_not_ulid() -> None:
    gid = grok_new_session_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", gid)
    assert "_" not in gid


def test_grok_empty_model_is_grok_46() -> None:
    assert grok_model(None) == "grok-4.6"
    assert grok_model("") == "grok-4.6"
    assert grok_model("  ") == "grok-4.6"
    assert grok_model("opus") == "opus"
    assert grok_model("grok-4.5") == "grok-4.5"


def test_grok_first_start_uses_session_id() -> None:
    argv = grok_launch_argv(existing="", model="", new_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert argv == [
        "grok",
        "--always-approve",
        "--session-id",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "--model",
        "grok-4.6",
    ]


def test_grok_resume_does_not_use_session_id() -> None:
    argv = grok_launch_argv(
        existing="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        model="grok-4.5",
        new_id="should-not-appear",
    )
    assert argv == [
        "grok",
        "--always-approve",
        "--resume",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "--model",
        "grok-4.5",
    ]
    assert "--session-id" not in argv
    assert "should-not-appear" not in argv


def test_grok_tmux_argv_unsets_claude_env() -> None:
    argv = grok_tmux_command_argv(existing="", model="", new_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert argv[:7] == [
        "env",
        "-u",
        "ANTHROPIC_API_KEY",
        "-u",
        "CLAUDECODE",
        "-u",
        "CLAUDE_CODE_ENTRYPOINT",
    ]
    assert argv[7:] == [
        "grok",
        "--always-approve",
        "--session-id",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "--model",
        "grok-4.6",
    ]


def test_grok_rejects_non_uuid_session_id() -> None:
    with pytest.raises(SystemExit, match="UUID"):
        grok_launch_argv(existing="", model="", new_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")


def test_start_cwd_argv() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(1, "", "no server")
        return Completed(0, "", "")

    rt = Runtime(runner=runner)
    rt.start("sess-1", None, None, None, cwd="/tmp/work")
    assert ["tmux", "new-session", "-d", "-s", "agent-sess-1", "-c", "/tmp/work"] in calls


def test_start_invalid_quoting_dies() -> None:
    rt = Runtime(runner=lambda argv: Completed(0, "tmux 3.3a", "") if argv[:2] == ["tmux", "-V"] else Completed(1, "", ""))
    with pytest.raises(SystemExit, match="invalid command quoting"):
        rt.start("sess-1", "'", None, None)


def test_start_without_tmux_dies() -> None:
    rt = Runtime(runner=lambda argv: Completed(127, "", "not found"))
    with pytest.raises(SystemExit, match="tmux is not installed"):
        rt.start("s", None, None, None)


def test_capture_missing_returns_empty() -> None:
    rt = Runtime(runner=lambda argv: Completed(1, "", "no session"))
    assert rt.capture("missing") == ""


def test_is_busy_when_grok_thinking() -> None:
    def runner(argv: list[str]) -> Completed:
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, "  \u280b Thinking… 4.3s                    [stop]\n", "")
        return Completed(1, "", "")

    assert Runtime(runner=runner).is_busy("s1") is True


def test_is_busy_false_on_idle_prompt() -> None:
    idle = (
        "     Worked for 1.8s\n"
        "  \u2502 \u276f                                                                        \u2502\n"
        "  Shift+Tab:mode  \u2502  Ctrl+x:shortcuts\n"
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, idle, "")
        return Completed(1, "", "")

    assert Runtime(runner=runner).is_busy("s1") is False


def test_grok_working_false_when_session_missing() -> None:
    rt = Runtime(runner=lambda argv: Completed(1, "", "no session"))
    assert rt.grok_working("missing") is False


def test_run_argv_killing_tree_second_reap_timeout_kills_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the post-kill communicate() also times out, a SECOND killpg +
    proc.wait must run (lane._default_runner's third fallback tier, now
    shared via runtime.kill_process_group_and_reap)."""
    from agent_cli import runtime as runtime_mod

    killpg_calls: list[int] = []
    wait_calls: list[object] = []

    class FakeProc:
        pid = 4242
        returncode = None

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["x"], timeout=timeout)

        def wait(self, timeout=None):
            wait_calls.append(timeout)
            return None

    def fake_popen(*args, **kwargs):
        return FakeProc()

    def fake_killpg(pid, sig):
        killpg_calls.append(pid)

    monkeypatch.setattr(runtime_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_mod.os, "killpg", fake_killpg)

    result = runtime_mod.run_argv_killing_tree(["sleep", "999"], timeout=0.01)

    assert result.returncode == 124
    assert len(killpg_calls) == 2, "must kill the process group a second time"
    assert len(wait_calls) == 1, "must wait() once after the second kill"
