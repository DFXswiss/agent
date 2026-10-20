from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from websockets.exceptions import ConnectionClosed

from agent_cli import main as main_mod
from agent_cli.hub import HubError
from agent_cli.main import open_store


class _StopTest(Exception):
    pass


def _init_paired_store(tmp_path: Path) -> None:
    os.environ["AGENT_HOME"] = str(tmp_path)
    main_mod.main(["init"])
    store = open_store()
    try:
        store.set_meta("hub_url", "https://hub.example")
        store.set_meta("device_token", "fake-token")
    finally:
        store.close()


class _Hub:
    def __init__(self, ws: object) -> None:
        self._ws = ws

    def connect_sync_ws(self) -> object:
        return self._ws


def test_idle_connection_pushes_local_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the defect where `agent sync --follow` pushes local ledger events
    only when the hub sends a frame. An idle socket must reach _sync_once on
    the receive timeout. TimeoutError must not escape the session: it is an
    OSError, and cmd_sync treats OSError as a lost connection, so a leaked
    timeout would tear the socket down and reconnect on every idle interval."""
    syncs: list[object] = []

    class _IdleWs:
        def __init__(self) -> None:
            self.calls = 0

        def recv(self, timeout: float | None = None) -> str:
            # The deadline is armed before the loop, so the remaining time handed to
            # recv is just under SYNC_IDLE_SECONDS rather than exactly it.
            if timeout is None or not 0 < timeout <= main_mod.SYNC_IDLE_SECONDS:
                raise AssertionError(
                    f"recv timeout must be in (0, SYNC_IDLE_SECONDS], got {timeout!r}"
                )
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()
            raise _StopTest()

        def send(self, data: str) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(main_mod, "_sync_once", lambda store: syncs.append(store))
    monkeypatch.setattr(main_mod, "_publish_terminals", lambda *args, **kwargs: None)

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        runtime = object()
        with pytest.raises(_StopTest):
            main_mod._run_sync_ws_session(store, _Hub(_IdleWs()), runtime, {}, {}, {})
        assert len(syncs) >= 1, (
            "an idle timeout must push via _sync_once and must not propagate TimeoutError"
        )
    finally:
        store.close()


def test_closed_connection_surfaces_as_hub_error(tmp_path: Path) -> None:
    """Pins the contract that a closed sync websocket still surfaces as HubError
    once the receive loop no longer ends by falling off the iterator. cmd_sync
    already catches HubError and retries with backoff."""

    class _ClosedWs:
        def recv(self, timeout: float | None = None) -> str:
            raise ConnectionClosed(None, None)

        def send(self, data: str) -> None:
            return None

        def close(self) -> None:
            return None

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        runtime = object()
        with pytest.raises(HubError):
            main_mod._run_sync_ws_session(store, _Hub(_ClosedWs()), runtime, {}, {}, {})
    finally:
        store.close()


def test_steady_non_sync_traffic_still_reaches_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the defect where a stream of frames that do not themselves trigger a sync
    reset the receive timeout and starved the push."""
    syncs: list[object] = []

    class _SteadyWs:
        def __init__(self) -> None:
            self.calls = 0

        def recv(self, timeout: float | None = None) -> str:
            self.calls += 1
            if self.calls <= 3:
                return json.dumps({"type": "control-ready"})
            raise _StopTest()

        def send(self, data: str) -> None:
            return None

        def close(self) -> None:
            return None

    # established["at"], then the deadline arm, then one monotonic() per remaining
    # check. The clock jumps past SYNC_IDLE_SECONDS between receives; recv itself
    # never raises TimeoutError. Spare values keep the iterator from running out.
    times = iter([0.0, 0.0, 0.0, 10.0, 20.0, 40.0, 40.0, 40.0, 70.0, 70.0, 100.0, 100.0])
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: syncs.append(store))
    monkeypatch.setattr(main_mod, "_publish_terminals", lambda *args, **kwargs: None)

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        runtime = object()
        with pytest.raises(_StopTest):
            main_mod._run_sync_ws_session(store, _Hub(_SteadyWs()), runtime, {}, {}, {})
        assert len(syncs) >= 1, (
            "a stream of non-sync frames must still reach _sync_once on the deadline"
        )
    finally:
        store.close()


def test_deadline_is_rearmed_before_terminals_are_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the drift where the deadline was re-armed after `_publish_terminals`,
    so the publication time was added to every interval."""
    publication = 5.0
    syncs: list[float] = []
    timeouts: list[float | None] = []

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def monotonic(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    class _TimeoutWs:
        def __init__(self) -> None:
            self.calls = 0

        def recv(self, timeout: float | None = None) -> str:
            timeouts.append(timeout)
            self.calls += 1
            if self.calls <= 2:
                raise TimeoutError()
            raise _StopTest()

        def send(self, data: str) -> None:
            return None

        def close(self) -> None:
            return None

    clock = _Clock()
    monkeypatch.setattr(main_mod.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(
        main_mod, "_sync_once", lambda store: syncs.append(clock.monotonic())
    )
    monkeypatch.setattr(
        main_mod,
        "_publish_terminals",
        lambda *args, **kwargs: clock.advance(publication),
    )

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        runtime = object()
        with pytest.raises(_StopTest):
            main_mod._run_sync_ws_session(store, _Hub(_TimeoutWs()), runtime, {}, {}, {})
        assert len(syncs) >= 2, "the idle loop must reach _sync_once twice"
        # Publication takes 5s. Re-arming before it folds that into the interval, so
        # the next recv waits SYNC_IDLE_SECONDS minus 5. Re-arming after would hand
        # recv the full SYNC_IDLE_SECONDS and add those 5s to every push gap.
        assert timeouts[1] is not None
        assert abs(timeouts[1] - (main_mod.SYNC_IDLE_SECONDS - publication)) < 1e-9
    finally:
        store.close()
