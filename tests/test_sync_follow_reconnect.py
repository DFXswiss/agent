from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import main as main_mod
from agent_cli.main import cmd_sync, open_store
from agent_cli.hub import HubError


class _StopTest(Exception):
    pass


def _init_paired_store(tmp_path: Path) -> None:
    os.environ["AGENT_HOME"] = str(tmp_path)
    main_mod.main(["init"])
    store = open_store()
    try:
        store.set_meta("hub_url", "https://agent.dfx.swiss")
        store.set_meta("device_token", "fake-token")
    finally:
        store.close()


def test_follow_reconnects_after_connection_drop_instead_of_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the 2026-08-23 bug: a dropped sync websocket used to
    call die() (SystemExit), permanently killing the persistent `agent sync
    --follow` listener. Any control frame queued by the hub while the client
    was down was then silently lost forever - no crash, no visible error,
    nothing ever executed device-side. The fix wraps the per-connection
    session in a reconnect loop with backoff instead of exiting."""

    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(store, hub, runtime, terminal_seq, last_capture, backoff):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("connection reset by peer")
        raise _StopTest("reconnected successfully - stopping the test here")

    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert len(calls) == 2, "client must reconnect after the first dropped connection, not exit"
    assert sleeps == [1.0], "must back off before retrying, starting at 1s"


def test_follow_used_to_die_on_first_disconnect_before_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Characterizes the pre-fix behavior directly: if _run_sync_ws_session
    always raises, the OLD implementation would have propagated die()'s
    SystemExit out of cmd_sync on the very first failure. The fix must not
    let that happen - it should keep retrying (bounded here by call count)."""

    calls: list[int] = []

    def always_fails(store, hub, runtime, terminal_seq, last_capture, backoff):
        calls.append(1)
        if len(calls) >= 3:
            raise _StopTest("stop after 3 attempts")
        raise HubError("hub websocket failed: simulated")

    monkeypatch.setattr(main_mod, "_run_sync_ws_session", always_fails)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert calls == [1, 1, 1]


class _EmptyWs:
    """A websocket that accepts sends and yields no incoming messages."""

    def send(self, data: str) -> None:
        pass

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        pass


class _FakeRuntime:
    """Stands in for Runtime without shelling out to tmux."""

    def capture(self, session_id: str) -> str:
        return "some output"


def test_publish_terminals_send_failure_reconnects_instead_of_dying(
    tmp_path: Path,
) -> None:
    """Regression test: a failed terminal-frame send inside _publish_terminals used
    to call die() (SystemExit), which cmd_sync's reconnect handler does not catch -
    killing the persistent --follow process the moment an attached session tried to
    publish a frame, instead of reconnecting. It must now propagate as an ordinary
    exception so the reconnect loop's except clause catches it."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:
        store.write(
            "session",
            "insert",
            "s1",
            {"id": "s1", "runtime": {"control": "attached"}},
        )

        class _FailOnTerminalSendWs:
            def __init__(self) -> None:
                self.sends = 0

            def send(self, data: str) -> None:
                self.sends += 1
                if self.sends == 1:
                    return  # control-ready
                raise OSError("broken pipe")  # terminal publish

            def __iter__(self):
                return iter(())

            def close(self) -> None:
                pass

        class _FakeHub:
            def connect_sync_ws(self) -> _FailOnTerminalSendWs:
                return _FailOnTerminalSendWs()

        with pytest.raises(OSError):
            main_mod._run_sync_ws_session(store, _FakeHub(), _FakeRuntime(), {}, {}, [1.0])
    finally:
        store.close()


def test_backoff_resets_after_successful_reconnect(tmp_path: Path) -> None:
    """Regression test: backoff must reset once a new connection is established
    (control-ready sent successfully), not stay pinned at whatever it grew to during
    earlier failures. _run_sync_ws_session never returns normally - it always raises
    on the connection ending - so a reset placed only after its call site returns was
    unreachable dead code."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:

        class _FakeHub:
            def connect_sync_ws(self) -> _EmptyWs:
                return _EmptyWs()

        backoff = [16.0]  # simulate having grown from earlier failed attempts
        with pytest.raises(HubError):
            main_mod._run_sync_ws_session(store, _FakeHub(), _FakeRuntime(), {}, {}, backoff)
        assert backoff[0] == 1.0, "backoff must reset once the new connection is established"
    finally:
        store.close()
