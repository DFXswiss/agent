from __future__ import annotations

from agent_cli.keep_working import CONTINUE, STANDING, tick
from agent_cli.runtime import Completed, Runtime


class FakeRuntime:
    def __init__(self, *, exists: bool = True, pane: str = "") -> None:
        self._exists = exists
        self.pane = pane
        self.texts: list[str] = []
        self.keys: list[str] = []

    def exists(self, session_id: str, *, target: str | None = None) -> bool:
        return self._exists

    def capture(self, session_id: str) -> str:
        return self.pane

    def input_text(self, session_id: str, data: str, *, target: str | None = None) -> None:
        self.texts.append(data)

    def input_key(self, session_id: str, key: str, *, target: str | None = None) -> None:
        self.keys.append(key)


def test_missing_session_is_noop() -> None:
    rt = FakeRuntime(exists=False)
    assert tick(rt, "worker", {}) == "missing"
    assert rt.texts == []
    assert rt.keys == []


def test_working_is_noop() -> None:
    rt = FakeRuntime(pane="    Waiting for response… 1s   [stop]\n")
    assert tick(rt, "worker", {}) == "working"
    assert rt.texts == []


def test_permission_sends_enter_only() -> None:
    pane = (
        "  1 (●) Yes, and don't ask again for anything (always-approve mode)\n"
        "  1/3:select  │  Tab:next option\n"
    )
    rt = FakeRuntime(pane=pane)
    assert tick(rt, "worker", {}) == "approved"
    assert rt.texts == []
    assert rt.keys == ["enter"]


def test_first_idle_sends_standing_once() -> None:
    rt = FakeRuntime(pane="     Worked for 1.8s\n  │ ❯\n")
    state: dict[str, object] = {}
    assert tick(rt, "worker", state) == "standing"
    assert state["standing_sent"] is True
    assert rt.texts == [STANDING]
    assert rt.keys == ["enter"]
    rt.texts.clear()
    rt.keys.clear()
    assert tick(rt, "worker", state) == "continue"
    assert rt.texts == [CONTINUE]
    assert rt.keys == ["enter"]


def test_empty_pane_does_not_type() -> None:
    rt = FakeRuntime(pane="")
    assert tick(rt, "worker", {}) == "unobservable"
    assert rt.texts == []
    assert rt.keys == []


def test_pane_without_composer_is_not_idle() -> None:
    rt = FakeRuntime(pane="dashboard only\nShift+Tab:mode\n")
    assert tick(rt, "worker", {}) == "working"
    assert rt.texts == []


def test_runtime_argv_unchanged_for_keep_working_helpers() -> None:
    # Keep the module importable next to Runtime without needing tmux.
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    Runtime(runner=runner)
    assert calls == []
