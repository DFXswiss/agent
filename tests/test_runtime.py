from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent_cli.drivers import lane_scrub_env
from agent_cli.runtime import (
    TARGETS_FILE,
    Completed,
    PreflightRuntime,
    Runtime,
    interactive_command_argv,
    interactive_launch_argv,
    interactive_model,
    load_tmux_targets,
    new_interactive_session_id,
    tmux_name,
)
from agent_cli.store import StoreError
from driver_fixtures import PANE_IDLE, PANE_WORKING, driver_specs, pane_markers


DRIVERS = driver_specs()
SPEC_A = DRIVERS["driver-a"].interactive
assert SPEC_A is not None


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
    assert rt.start("sess-1", None, None, None) is True
    assert ["tmux", "new-session", "-d", "-s", "agent-sess-1"] in calls

    calls.clear()
    rt = Runtime(runner=runner)
    assert rt.start("sess-1", "bash -l", 80, 24) is True
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
    assert rt.start("sess-1", None, 100, 40) is False
    assert ["tmux", "new-session", "-d", "-s", "agent-sess-1"] not in calls
    assert ["tmux", "resize-window", "-t", "agent-sess-1", "-x", "100", "-y", "40"] in calls

    calls.clear()
    rt = Runtime(runner=runner_exists)
    assert rt.start("sess-1", None, 100, 40, require_new=True) is False
    assert not any(c[:2] == ["tmux", "new-session"] for c in calls)
    assert not any(c[:2] == ["tmux", "resize-window"] for c in calls)

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


def test_interactive_session_id_is_uuid_not_ulid() -> None:
    sid = new_interactive_session_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", sid)
    assert "_" not in sid


def test_interactive_model_none_or_blank_raises_store_error() -> None:
    with pytest.raises(StoreError, match="explicitly configured"):
        interactive_model(None)
    with pytest.raises(StoreError, match="explicitly configured"):
        interactive_model("")
    with pytest.raises(StoreError, match="explicitly configured"):
        interactive_model("  ")
    assert interactive_model("operator-selected-model") == "operator-selected-model"
    assert interactive_model("  operator-selected-model  ") == "operator-selected-model"


def test_interactive_first_start_uses_the_new_session_template() -> None:
    argv = interactive_launch_argv(
        SPEC_A,
        existing="",
        model="operator-selected-model",
        new_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    assert argv == [
        "tool-a",
        "--session-id",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "--model",
        "operator-selected-model",
    ]


def test_interactive_resume_uses_the_resume_template() -> None:
    argv = interactive_launch_argv(
        SPEC_A,
        existing="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        model="operator-resume-model",
        new_id="should-not-appear",
    )
    assert argv == [
        "tool-a",
        "--resume",
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "--model",
        "operator-resume-model",
    ]
    assert "--session-id" not in argv
    assert "should-not-appear" not in argv


def test_interactive_command_argv_unsets_every_scrub_variable() -> None:
    scrub = lane_scrub_env(DRIVERS)
    argv = interactive_command_argv(
        SPEC_A,
        existing="",
        model="operator-selected-model",
        new_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        scrub=scrub,
    )
    expected = ["env"]
    for key in scrub:
        expected.extend(["-u", key])
    expected.extend(
        [
            "tool-a",
            "--session-id",
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "--model",
            "operator-selected-model",
        ]
    )
    assert argv == expected
    assert {"TOOL_A_API_KEY", "TOOL_C_TOKEN", "HTTPS_PROXY"} <= set(scrub)


def test_interactive_start_rejects_non_uuid_session_id() -> None:
    with pytest.raises(SystemExit, match="UUID"):
        interactive_launch_argv(
            SPEC_A,
            existing="",
            model="operator-selected-model",
            new_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )

    with pytest.raises(StoreError, match="explicitly configured"):
        interactive_launch_argv(
            SPEC_A,
            existing="",
            model="",
            new_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )


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


def _pane_runner(pane: str):
    def runner(argv: list[str]) -> Completed:
        if argv[:2] == ["tmux", "has-session"]:
            return Completed(0, "", "")
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, pane, "")
        return Completed(1, "", "")

    return runner


def test_is_busy_when_the_pane_shows_a_working_marker() -> None:
    classified: list[str] = []

    def markers_for(session_id: str):
        classified.append(session_id)
        return pane_markers("driver-a")

    rt = Runtime(runner=_pane_runner(PANE_WORKING), markers_for=markers_for)
    assert rt.is_busy("s1") is True
    assert classified == ["s1"]


def test_is_busy_false_on_idle_prompt() -> None:
    rt = Runtime(runner=_pane_runner(PANE_IDLE), markers_for=lambda _sid: pane_markers())
    assert rt.is_busy("s1") is False


def test_pane_working_false_when_session_missing() -> None:
    rt = Runtime(runner=lambda argv: Completed(1, "", "no session"))
    assert rt.pane_working("missing") is False


def test_pane_classification_without_markers_fails_loudly() -> None:
    rt = Runtime(runner=_pane_runner(PANE_WORKING))
    with pytest.raises(StoreError, match="no pane markers"):
        rt.is_busy("s1")


def test_preflight_runtime_resolves_its_own_markers() -> None:
    inner = Runtime(runner=_pane_runner(PANE_WORKING))
    rt = PreflightRuntime(inner, lambda _sid: None, markers_for=lambda _sid: pane_markers())
    assert rt.is_busy("s1") is True
    delegated = PreflightRuntime(
        Runtime(runner=_pane_runner(PANE_IDLE), markers_for=lambda _sid: pane_markers()),
        lambda _sid: None,
    )
    assert delegated.is_busy("s1") is False


def _recording_runner(calls: list[list[str]]) -> object:
    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[-2:] == ["tmux", "-V"] or argv[:2] == ["tmux", "-V"]:
            return Completed(0, "tmux 3.3a", "")
        if "has-session" in argv:
            return Completed(1, "", "no server")
        return Completed(0, "", "")

    return runner


def test_tmux_prefix_prepended_per_session() -> None:
    calls: list[list[str]] = []
    prefix = ["remote-exec", "-T", "worker-a"]
    rt = Runtime(runner=_recording_runner(calls), tmux_targets={"worker-a": prefix})
    assert rt.available("worker-a") is True
    assert prefix + ["tmux", "-V"] in calls
    calls.clear()
    rt.start("worker-a", None, None, None)
    assert prefix + ["tmux", "has-session", "-t", "agent-worker-a"] in calls
    assert prefix + ["tmux", "new-session", "-d", "-s", "agent-worker-a"] in calls
    calls.clear()
    rt.input_text("worker-a", "hello")
    assert prefix + ["tmux", "send-keys", "-t", "agent-worker-a", "-l", "--", "hello"] in calls
    calls.clear()
    rt.capture("worker-a")
    assert prefix + [
        "tmux",
        "capture-pane",
        "-t",
        "agent-worker-a",
        "-p",
        "-e",
        "-J",
    ] in calls
    calls.clear()

    def runner_alive(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "has-session" in argv:
            return Completed(0, "", "")
        return Completed(0, "", "")

    rt = Runtime(runner=runner_alive, tmux_targets={"worker-a": prefix})
    rt.stop("worker-a")
    assert prefix + ["tmux", "kill-session", "-t", "agent-worker-a"] in calls
    calls.clear()
    rt.resize("worker-a", 80, 24)
    assert prefix + ["tmux", "resize-window", "-t", "agent-worker-a", "-x", "80", "-y", "24"] in calls


def test_one_runtime_two_sessions_different_prefixes() -> None:
    calls: list[list[str]] = []
    rt = Runtime(
        runner=_recording_runner(calls),
        tmux_targets={
            "worker-a": ["exec-a"],
            "worker-b": ["exec-b", "-T"],
        },
    )
    rt.available("worker-a")
    rt.available("worker-b")
    assert ["exec-a", "tmux", "-V"] in calls
    assert ["exec-b", "-T", "tmux", "-V"] in calls
    calls.clear()
    rt.start("local-sess", None, None, None)
    assert ["tmux", "new-session", "-d", "-s", "agent-local-sess"] in calls
    assert not any(c[:1] == ["exec-a"] and "local-sess" in c[-1] for c in calls)


def test_missing_target_key_stays_unprefixed() -> None:
    calls: list[list[str]] = []
    rt = Runtime(runner=_recording_runner(calls), tmux_targets={"other": ["nope"]})
    rt.start("sess-1", None, None, None)
    assert ["tmux", "new-session", "-d", "-s", "agent-sess-1"] in calls
    assert all(c[:1] != ["nope"] for c in calls)


def test_load_tmux_targets_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_tmux_targets(tmp_path) == {}


def test_load_tmux_targets_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / TARGETS_FILE).write_text("{", encoding="utf-8")
    with pytest.raises(StoreError, match="not valid JSON"):
        load_tmux_targets(tmp_path)


def test_load_tmux_targets_rejects_non_object(tmp_path: Path) -> None:
    (tmp_path / TARGETS_FILE).write_text("[]", encoding="utf-8")
    with pytest.raises(StoreError, match="must be an object"):
        load_tmux_targets(tmp_path)


def test_load_tmux_targets_rejects_non_list_value(tmp_path: Path) -> None:
    (tmp_path / TARGETS_FILE).write_text('{"worker-a": "exec"}', encoding="utf-8")
    with pytest.raises(StoreError, match="array of strings"):
        load_tmux_targets(tmp_path)


def test_load_tmux_targets_rejects_empty_string_element(tmp_path: Path) -> None:
    (tmp_path / TARGETS_FILE).write_text('{"worker-a": ["exec", ""]}', encoding="utf-8")
    with pytest.raises(StoreError, match="array of strings"):
        load_tmux_targets(tmp_path)


def test_load_tmux_targets_accepts_empty_prefix_list(tmp_path: Path) -> None:
    (tmp_path / TARGETS_FILE).write_text('{"worker-a": []}', encoding="utf-8")
    assert load_tmux_targets(tmp_path) == {"worker-a": []}


def test_load_tmux_targets_rejects_directory(tmp_path: Path) -> None:
    (tmp_path / TARGETS_FILE).mkdir()
    with pytest.raises(StoreError, match="is not a file"):
        load_tmux_targets(tmp_path)
