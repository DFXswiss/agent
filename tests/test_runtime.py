from __future__ import annotations

import pytest

from agent_cli.runtime import Completed, Runtime, tmux_name
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


def test_start_without_tmux_dies() -> None:
    rt = Runtime(runner=lambda argv: Completed(127, "", "not found"))
    with pytest.raises(SystemExit, match="tmux is not installed"):
        rt.start("s", None, None, None)


def test_capture_missing_returns_empty() -> None:
    rt = Runtime(runner=lambda argv: Completed(1, "", "no session"))
    assert rt.capture("missing") == ""
