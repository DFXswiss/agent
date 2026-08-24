from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from websockets.exceptions import WebSocketException

from agent_cli import main as main_mod
from agent_cli.main import cmd_sync, open_store
from agent_cli.hub import HubError
from agent_cli.store import StoreConnectionError, StoreError


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

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
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

    def always_fails(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
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


def test_initial_sync_failure_in_follow_mode_reconnects_instead_of_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the very first sync attempt when starting `agent sync
    --follow` used to run unprotected before the reconnect loop even began - if the
    hub was unreachable at process start (e.g. a service manager restarting the
    agent and the hub together), that first _sync_once call killed the process
    before any of this PR's reconnect-with-backoff logic ever ran. It must now go
    through the same retry loop as every subsequent sync."""
    calls: list[int] = []

    def fake_sync_once(store: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise HubError("hub unreachable at startup")
        raise _StopTest("reconnected past the initial failure - stopping the test here")

    monkeypatch.setattr(main_mod, "_sync_once", fake_sync_once)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert len(calls) == 2, "the first sync failure must be retried, not exit the process"


def test_hub_construction_failure_in_follow_mode_reconnects_instead_of_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: _hub_from_store used to run unprotected before the reconnect
    loop even began (once follow mode passed the initial sync check) - it calls
    store.meta(), one of the wrapped methods, so a transient DB blip right there
    would kill the process before the loop's own retry logic ever got a chance to
    run, the same failure class already fixed for the initial sync. It must now be
    constructed lazily inside the loop so a failure there is retried too."""
    calls: list[int] = []

    def fake_hub_from_store(store: object) -> object:
        calls.append(1)
        if len(calls) == 1:
            raise StoreConnectionError("hub metadata lookup failed: connection lost")
        raise _StopTest("reconnected past the hub construction failure - stopping the test here")

    monkeypatch.setattr(main_mod, "_hub_from_store", fake_hub_from_store)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert len(calls) == 2, "a hub-construction failure must be retried, not exit the process"


def test_store_connection_error_from_sync_reconnects_instead_of_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a lost/reset local postgres connection surfaces as
    psycopg.Error, which Store's reconnect-path methods translate into
    StoreConnectionError (see tests/test_store.py). The reconnect loop's except
    clause must actually include StoreConnectionError - otherwise a transient DB
    blip during --follow, not just a hub disconnect, would still kill the process."""
    calls: list[int] = []

    def fake_sync_once(store: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise StoreConnectionError("postgres error: server closed the connection unexpectedly")
        raise _StopTest("reconnected past the store error - stopping the test here")

    monkeypatch.setattr(main_mod, "_sync_once", fake_sync_once)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert len(calls) == 2, "a StoreConnectionError from sync must be retried, not exit the process"


def test_reconnect_loop_reopens_the_store_connection_on_store_connection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: catching StoreConnectionError without ever reopening the
    connection just turns a clean crash into a permanent silent stall - every retry
    would hit the same dead connection and fail identically forever. The reconnect
    loop must actually call store.reconnect() so a later attempt can succeed once
    postgres is back."""
    reconnect_calls: list[int] = []
    calls: list[int] = []

    def fake_sync_once(store: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise StoreConnectionError("server closed the connection unexpectedly")
        raise _StopTest("reconnected past the store error - stopping the test here")

    monkeypatch.setattr(main_mod, "_sync_once", fake_sync_once)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        monkeypatch.setattr(store, "reconnect", lambda: reconnect_calls.append(1))
        monkeypatch.setattr(main_mod, "open_store", lambda *a, **k: store)

        with pytest.raises(_StopTest):
            cmd_sync(["--follow"])

        assert reconnect_calls == [1], "store.reconnect() must be called exactly once per StoreConnectionError"
    finally:
        store.close()


def test_reconnect_failure_is_logged_and_retried_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: if store.reconnect() itself fails (postgres still
    unreachable), that failure must be logged and the loop must keep retrying -
    not let the reconnect attempt's own exception escape and crash the process,
    which would be exactly the bug this whole PR exists to fix, triggered by the
    recovery path itself."""
    calls: list[int] = []

    def fake_sync_once(store: object) -> None:
        calls.append(1)
        if len(calls) >= 3:
            raise _StopTest("stop after enough attempts")
        raise StoreConnectionError("server closed the connection unexpectedly")

    def failing_reconnect() -> None:
        raise StoreConnectionError("cannot reconnect to postgres: connection refused")

    monkeypatch.setattr(main_mod, "_sync_once", fake_sync_once)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        monkeypatch.setattr(store, "reconnect", failing_reconnect)
        monkeypatch.setattr(main_mod, "open_store", lambda *a, **k: store)

        with pytest.raises(_StopTest):
            cmd_sync(["--follow"])

        assert calls == [1, 1, 1], "a failed reconnect attempt must not crash the process, only be retried"
    finally:
        store.close()


def test_backoff_does_not_credit_time_spent_reconnecting_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the elapsed-time-since-established check used to call
    time.monotonic() AFTER store.reconnect() returned, so if reconnecting the
    postgres connection itself took a long time (psycopg.connect() sets no
    explicit timeout), that recovery time got credited toward "the session was
    stable" and could falsely reset backoff - even though the actual
    established-session lifetime was near zero. The elapsed check must be measured
    from right when the failure was caught, before any recovery work runs."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        established["at"] = main_mod.time.monotonic()
        calls.append(1)
        if len(calls) >= 3:
            raise _StopTest("stop after enough attempts")
        raise StoreConnectionError("connection reset by peer")

    def slow_reconnect() -> None:
        main_mod.time.monotonic()  # simulate reconnect() itself consuming meaningful time

    # iter1: established at t=0.0, elapsed check at t=0.1 (small gap, no reset) ->
    #        reconnect()'s own call consumes t=2.0, which must be ignored.
    # iter2: established at t=1.0, elapsed check at t=1.1 (small gap, no reset) ->
    #        reconnect()'s own call consumes t=999.0 - if that leaked into the
    #        elapsed check instead, it would look like a 998s-stable session and
    #        wrongly reset backoff.
    times = iter([0.0, 0.1, 2.0, 1.0, 1.1, 999.0, 5.0])
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))

    _init_paired_store(tmp_path)
    store = open_store()
    try:
        monkeypatch.setattr(store, "reconnect", slow_reconnect)
        monkeypatch.setattr(main_mod, "open_store", lambda *a, **k: store)

        with pytest.raises(_StopTest):
            cmd_sync(["--follow"])

        assert sleeps == [1.0, 2.0], (
            "backoff must keep growing across both attempts - reconnect()'s own "
            "elapsed time must never be mistaken for a stable established session"
        )
    finally:
        store.close()


def test_generic_store_error_from_sync_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a StoreError raised for a genuine data problem (a
    conflicting event, an origin_seq gap - see test_write_emits_seq_and_blocks_foreign
    and test_remote_gap_fail_closed in tests/test_store.py) is not a connection
    issue and must not be silently retried forever as if it were one - it needs to
    keep failing loud so a human notices the actual data corruption."""
    calls: list[int] = []

    def fake_sync_once(store: object) -> None:
        calls.append(1)
        raise StoreError("conflicting event other-device seq 3")

    monkeypatch.setattr(main_mod, "_sync_once", fake_sync_once)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)

    with pytest.raises(StoreError, match="conflicting event"):
        cmd_sync(["--follow"])

    assert calls == [1], "a genuine data-integrity StoreError must not be retried"


def test_subscription_row_store_connection_error_reaches_the_reconnect_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a lost DB connection encountered while replicating an
    incoming subscription row must reach cmd_sync's reconnect loop, not be silently
    swallowed by the pre-existing `except (StoreError, KeyError, TypeError):
    continue` around store.apply_replica_row - that clause exists for per-row
    validation failures (a malformed row), not connection loss, but
    apply_replica_row is one of the methods that now translates psycopg.Error into
    a StoreError subclass."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:

        def broken_apply_replica_row(row: dict) -> None:
            raise StoreConnectionError("server closed the connection unexpectedly")

        monkeypatch.setattr(store, "apply_replica_row", broken_apply_replica_row)

        class _SubscriptionWs:
            def send(self, data: str) -> None:
                pass

            def __iter__(self):
                return iter(
                    [json.dumps({"type": "subscription", "rows": [{"table": "session", "id": "s1"}]})]
                )

            def close(self) -> None:
                pass

        class _FakeHub:
            def connect_sync_ws(self) -> _SubscriptionWs:
                return _SubscriptionWs()

        with pytest.raises(StoreConnectionError):
            main_mod._run_sync_ws_session(store, _FakeHub(), _FakeRuntime(), {}, {}, {})
    finally:
        store.close()


def test_apply_control_propagates_store_connection_error_instead_of_acking_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: apply_control's `except (SystemExit, StoreError, ValueError)`
    used to catch StoreConnectionError too (a StoreError subclass), turning a lost
    DB connection into a normal-looking control-ack with ok=False instead of letting
    it reach cmd_sync's reconnect loop - so a control frame hit during a transient
    connection blip would fail permanently even though the connection later
    recovers, instead of being retried after reconnect like everything else."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:
        store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})

        def broken_device_id() -> str:
            raise StoreConnectionError("server closed the connection unexpectedly")

        monkeypatch.setattr(store, "device_id", broken_device_id)

        message = {"type": "control", "session_id": "s1", "action": "stop", "payload": {}}

        with pytest.raises(StoreConnectionError):
            main_mod.apply_control(store, _FakeRuntime(), message)
    finally:
        store.close()


def test_websocket_exception_reconnects_instead_of_dying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the reconnect handler's except tuple names WebSocketException
    explicitly - the type the websockets library actually raises for a real
    protocol-level disconnect - but nothing in the suite drove that specific type
    through the loop before this. Pin it down directly."""
    calls: list[int] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise WebSocketException("connection closed")
        raise _StopTest("reconnected after a websocket exception - stopping the test here")

    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: None)

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert len(calls) == 2, "a WebSocketException must be retried, not exit the process"


class _FakeRuntime:
    """Stands in for Runtime without shelling out to tmux."""

    def capture(self, session_id: str) -> str:
        return "some output"


def test_publish_terminals_send_failure_propagates_oserror_instead_of_die(
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

        established: dict[str, float] = {}
        with pytest.raises(OSError):
            main_mod._run_sync_ws_session(store, _FakeHub(), _FakeRuntime(), {}, {}, established)
        assert established.get("at") is not None, (
            "control-ready succeeded before the terminal publish failed; established must "
            "already be set by then, not only after a later step that can itself fail"
        )
    finally:
        store.close()


def test_established_not_set_when_the_connect_attempt_itself_fails(
    tmp_path: Path,
) -> None:
    """Regression test: established["at"] must not be set when hub.connect_sync_ws()
    itself fails - crediting a failed connect attempt as "stable" would let a hub
    whose connect attempts hang for a long time before failing (a black-holed
    connection, a slow TLS handshake - hub.connect_sync_ws() sets no explicit
    timeout) falsely reset backoff, the same failure class as the _sync_once case
    one call-frame deeper."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:

        class _FailToConnectHub:
            def connect_sync_ws(self) -> None:
                raise HubError("hub websocket failed: connection timed out")

        established: dict[str, float] = {}
        with pytest.raises(HubError):
            main_mod._run_sync_ws_session(
                store, _FailToConnectHub(), _FakeRuntime(), {}, {}, established
            )
        assert established == {}, "a failed connect must not be credited as an established session"
    finally:
        store.close()


def test_established_not_set_when_the_control_ready_send_itself_fails(
    tmp_path: Path,
) -> None:
    """Regression test: established["at"] must not be set when connect_sync_ws()
    succeeds but the initial control-ready send fails - the handshake isn't complete
    until that send succeeds, so crediting a connect-but-can't-send session as
    "stable" would be the same false-reset gap, one line later."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:

        class _FailOnFirstSendWs:
            def send(self, data: str) -> None:
                raise OSError("broken pipe")

            def close(self) -> None:
                pass

        class _FakeHub:
            def connect_sync_ws(self) -> _FailOnFirstSendWs:
                return _FailOnFirstSendWs()

        established: dict[str, float] = {}
        with pytest.raises(OSError):
            main_mod._run_sync_ws_session(store, _FakeHub(), _FakeRuntime(), {}, {}, established)
        assert established == {}, "a failed control-ready send must not be credited as established"
    finally:
        store.close()


def test_established_is_set_once_the_handshake_actually_succeeds(
    tmp_path: Path,
) -> None:
    """Regression test: the positive case - once connect_sync_ws() and the
    control-ready send both succeed, established["at"] must actually be set, so the
    caller can measure real connection stability from it."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:

        class _WorkingWs:
            def send(self, data: str) -> None:
                pass

            def __iter__(self):
                return iter(())

            def close(self) -> None:
                pass

        class _FakeHub:
            def connect_sync_ws(self) -> _WorkingWs:
                return _WorkingWs()

        established: dict[str, float] = {}
        with pytest.raises(HubError):
            main_mod._run_sync_ws_session(store, _FakeHub(), _FakeRuntime(), {}, {}, established)
        assert established.get("at") is not None, "a successful handshake must record when it happened"
    finally:
        store.close()


def test_publish_terminals_does_not_record_state_for_a_failed_send(
    tmp_path: Path,
) -> None:
    """Regression test: last_capture/terminal_seq must only be updated once the send
    actually succeeds. Recording state before sending meant a failed send still
    looked like a successful publish to the dedup check - and now that a reconnect
    keeps the process (and terminal_seq/last_capture) alive instead of restarting it,
    that frame would never be resent even after a successful reconnect."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:
        store.write(
            "session",
            "insert",
            "s1",
            {"id": "s1", "runtime": {"control": "attached"}},
        )

        class _FailOnceWs:
            def __init__(self) -> None:
                self.sent: list[str] = []
                self.fail_next = True

            def send(self, data: str) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise OSError("broken pipe")
                self.sent.append(data)

        terminal_seq: dict[str, int] = {}
        last_capture: dict[str, str] = {}
        ws = _FailOnceWs()

        with pytest.raises(OSError):
            main_mod._publish_terminals(store, _FakeRuntime(), ws, terminal_seq, last_capture)
        assert last_capture == {}, "a failed send must not be recorded as delivered"
        assert terminal_seq == {}

        main_mod._publish_terminals(store, _FakeRuntime(), ws, terminal_seq, last_capture)
        assert len(ws.sent) == 1, "the retried publish must actually resend the frame, not skip it"
        assert last_capture == {"s1": "some output"}
    finally:
        store.close()


def test_reconnect_republishes_terminal_state_even_if_unchanged(tmp_path: Path) -> None:
    """Regression test: terminal_seq/last_capture persist across internal reconnects
    (that's the whole point of this fix - the process survives instead of restarting
    with fresh dicts). But a brand new websocket connection has no idea what a prior,
    now-dead connection already sent - if the terminal output happens to be unchanged
    since the last publish on the old connection, the dedup check must not silently
    withhold it from the new connection forever."""
    _init_paired_store(tmp_path)
    store = open_store()
    try:
        store.write(
            "session",
            "insert",
            "s1",
            {"id": "s1", "runtime": {"control": "attached"}},
        )

        class _RecordingWs:
            def __init__(self) -> None:
                self.sent: list[str] = []

            def send(self, data: str) -> None:
                self.sent.append(data)

            def __iter__(self):
                return iter(())

            def close(self) -> None:
                pass

        class _FakeHub:
            def __init__(self) -> None:
                self.sessions: list[_RecordingWs] = []

            def connect_sync_ws(self) -> _RecordingWs:
                ws = _RecordingWs()
                self.sessions.append(ws)
                return ws

        hub = _FakeHub()
        terminal_seq: dict[str, int] = {}
        last_capture: dict[str, str] = {}

        with pytest.raises(HubError):
            main_mod._run_sync_ws_session(store, hub, _FakeRuntime(), terminal_seq, last_capture, {})
        assert len(hub.sessions[0].sent) == 2, "control-ready + one terminal frame"

        with pytest.raises(HubError):
            main_mod._run_sync_ws_session(store, hub, _FakeRuntime(), terminal_seq, last_capture, {})
        assert len(hub.sessions[1].sent) == 2, (
            "reconnect must republish the terminal even though its content is unchanged"
        )
    finally:
        store.close()


def test_backoff_grows_through_rapid_flapping_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: resetting backoff on every successful handshake regardless of
    how long the connection then survived would let a hub that accepts the connection
    and immediately drops it, every time, keep the retry delay pinned at ~1s forever
    instead of growing - a fast reconnect storm against the hub. Backoff must only
    reset after a connection proves itself stable, not merely established."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        established["at"] = main_mod.time.monotonic()  # simulate the handshake succeeding
        calls.append(1)
        if len(calls) >= 4:
            raise _StopTest("stop after 4 flapping attempts")
        raise OSError("connection reset by peer")

    times = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert sleeps == [1.0, 2.0, 4.0], "backoff must keep growing when every reconnect fails almost immediately"


def test_backoff_caps_at_max_backoff_under_sustained_flapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: backoff must stop growing once it reaches _MAX_BACKOFF, not
    keep doubling past it - nothing else in this suite drives the sequence far
    enough to observe the cap actually engage."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        established["at"] = main_mod.time.monotonic()
        calls.append(1)
        if len(calls) >= 8:
            raise _StopTest("stop after 8 flapping attempts")
        raise OSError("connection reset by peer")

    times = iter(i * 0.1 for i in range(16))
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, main_mod._MAX_BACKOFF, main_mod._MAX_BACKOFF], (
        "backoff must grow exponentially then cap at _MAX_BACKOFF, not keep doubling past it"
    )


def test_backoff_resets_after_a_stable_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: after a connection survives long enough to be considered
    healthy, the next failure must back off from scratch again, not inherit whatever
    backoff had grown to during earlier, unrelated flapping."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        established["at"] = main_mod.time.monotonic()  # simulate the handshake succeeding
        calls.append(1)
        if len(calls) >= 3:
            raise _StopTest("stop after 3 attempts")
        raise OSError("connection reset by peer")

    # attempt 1: starts at t=0.0, fails almost instantly (elapsed 0.1s) -> no reset
    # attempt 2: starts at t=1.0, fails after a long-lived session (elapsed 39s) -> reset
    times = iter([0.0, 0.1, 1.0, 40.0, 42.0])
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert sleeps == [1.0, 1.0], "backoff must reset to 1.0 after attempt 2's long-lived session, not grow to 2.0"


def test_backoff_resets_when_elapsed_equals_exactly_max_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the stability check is `elapsed >= _MAX_BACKOFF`, so a
    connection that survives for exactly _MAX_BACKOFF seconds must already count as
    stable. Pins down the inclusive boundary explicitly, since neither the threshold
    constant nor the >= vs > comparison is exercised elsewhere at this exact value."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        established["at"] = main_mod.time.monotonic()
        calls.append(1)
        if len(calls) >= 3:
            raise _StopTest("stop after 3 attempts")
        raise OSError("connection reset by peer")

    # attempt 1: fails almost instantly (elapsed 0.1s) -> no reset -> backoff grows to 2.0
    # attempt 2: fails after exactly _MAX_BACKOFF elapsed -> must still count as stable -> reset
    times = iter([0.0, 0.1, 1.0, 1.0 + main_mod._MAX_BACKOFF, 999.0])
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert sleeps == [1.0, 1.0], "elapsed == _MAX_BACKOFF must already reset backoff, not require exceeding it"


def test_backoff_does_not_reset_just_under_max_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the mirror of the exact-boundary case - a connection that
    falls just short of _MAX_BACKOFF must not be credited as stable."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_session(
        store: object,
        hub: object,
        runtime: object,
        terminal_seq: dict,
        last_capture: dict,
        established: dict,
    ) -> None:
        established["at"] = main_mod.time.monotonic()
        calls.append(1)
        if len(calls) >= 3:
            raise _StopTest("stop after 3 attempts")
        raise OSError("connection reset by peer")

    # attempt 1: fails almost instantly (elapsed 0.1s) -> no reset -> backoff grows to 2.0
    # attempt 2: fails just short of _MAX_BACKOFF elapsed -> must not count as stable -> no reset
    times = iter([0.0, 0.1, 1.0, 1.0 + main_mod._MAX_BACKOFF - 0.001, 999.0])
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fake_session)
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: None)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: next(times))

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert sleeps == [1.0, 2.0], "elapsed just under _MAX_BACKOFF must not reset backoff"


def test_backoff_does_not_reset_when_sync_once_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: the stability clock must start after _sync_once succeeds, not
    before it runs. _sync_once's hub client has the same 30s timeout as _MAX_BACKOFF -
    if a slow/timing-out sync alone (the websocket never even reached) counted toward
    the stability window, it would falsely reset backoff and reproduce the exact
    retry-storm the stability gate exists to prevent, just via a different trigger."""
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_sync_once(store: object) -> None:
        calls.append(1)
        if len(calls) >= 4:
            raise _StopTest("stop after enough attempts")
        raise HubError("sync request timed out")  # websocket never reached

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("time.monotonic() must not be consulted when _sync_once itself failed")

    monkeypatch.setattr(main_mod, "_sync_once", fake_sync_once)
    monkeypatch.setattr(main_mod, "_run_sync_ws_session", fail_if_called)
    monkeypatch.setattr(main_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(main_mod.time, "monotonic", fail_if_called)

    _init_paired_store(tmp_path)

    with pytest.raises(_StopTest):
        cmd_sync(["--follow"])

    assert sleeps == [1.0, 2.0, 4.0], "backoff must keep growing while only _sync_once is failing"
