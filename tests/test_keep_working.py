from __future__ import annotations

from agent_cli.drivers import PaneMarkers
from agent_cli.keep_working import CONTINUE, STANDING, tick
from agent_cli.runtime import Completed, Runtime
from driver_fixtures import PANE_IDLE, PANE_PERMISSION, PANE_WORKING, pane_markers


class FakeRuntime:
    def __init__(self, *, exists: bool = True, pane: str = "") -> None:
        self._exists = exists
        self.pane = pane
        self.texts: list[str] = []
        self.keys: list[str] = []
        self.classified: list[str] = []

    def exists(self, session_id: str, *, target: str | None = None) -> bool:
        return self._exists

    def markers_for(self, session_id: str) -> PaneMarkers:
        self.classified.append(session_id)
        return pane_markers("driver-a")

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
    rt = FakeRuntime(pane=PANE_WORKING)
    assert tick(rt, "worker", {}) == "working"
    assert rt.texts == []
    assert rt.classified == ["worker"]


def test_permission_sends_enter_only() -> None:
    rt = FakeRuntime(pane=PANE_PERMISSION)
    assert tick(rt, "worker", {}) == "approved"
    assert rt.texts == []
    assert rt.keys == ["enter"]


def test_first_idle_sends_standing_once() -> None:
    rt = FakeRuntime(pane=PANE_IDLE)
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
    assert rt.classified == []


def test_pane_without_composer_is_not_idle() -> None:
    rt = FakeRuntime(pane="dashboard only\nStatus bar: model-a\n")
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
