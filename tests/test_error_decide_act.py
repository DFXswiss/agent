from __future__ import annotations

import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_cli.error_decide_act import (
    _ensure_decide_session,
    _has_conclusion,
    _wait_for_conclusion,
    decide_session_id,
    scan_error_decide,
    unconcluded_seen_rows,
)
from agent_cli.store import Store, StoreError, utcnow


def _insert_seen(
    store: Store,
    *,
    rid: str,
    session_id: str = "scan-1",
    first_seen: str = "2026-08-23T16:00:00Z",
    fingerprint: str = "api|TimeoutError|abc|prod",
) -> None:
    store.write(
        "activity",
        "insert",
        rid,
        {
            "id": rid,
            "session_id": session_id,
            "type": "error.seen",
            "payload": {
                "fingerprint": fingerprint,
                "first_seen": first_seen,
                "repo": "org/app",
            },
            "execution_status": "done",
        },
    )


def _insert_conclusion(
    store: Store,
    *,
    rid: str,
    error_id: str,
    typ: str = "error.fix",
    session_id: str = "decide-1",
) -> None:
    payload: dict = {
        "error_id": error_id,
        "fingerprint": "api|TimeoutError|abc|prod",
    }
    if typ == "error.skip":
        payload["reason"] = "noisy"
    store.write(
        "activity",
        "insert",
        rid,
        {
            "id": rid,
            "session_id": session_id,
            "type": typ,
            "payload": payload,
            "execution_status": "pending" if typ == "error.fix" else "done",
        },
    )


def test_unconcluded_seen_rows_orders_by_first_seen_not_id(tmp_path: Path) -> None:
    store = Store(tmp_path)
    # ids sort as aaa < zzz, but first_seen timestamps go the other way
    _insert_seen(store, rid="zzz-newer", first_seen="2026-08-23T17:00:00Z")
    _insert_seen(store, rid="aaa-older", first_seen="2026-08-23T15:00:00Z")
    _insert_seen(store, rid="mmm-mid", first_seen="2026-08-23T16:00:00Z")
    rows = unconcluded_seen_rows(store)
    assert [row["id"] for row in rows] == ["aaa-older", "mmm-mid", "zzz-newer"]


def test_has_conclusion_true_and_false(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_seen(store, rid="seen-open")
    _insert_seen(store, rid="seen-closed")
    assert _has_conclusion(store, "seen-open") is False
    assert _has_conclusion(store, "seen-closed") is False
    _insert_conclusion(store, rid="fix-1", error_id="seen-closed", typ="error.skip")
    assert _has_conclusion(store, "seen-closed") is True
    assert _has_conclusion(store, "seen-open") is False


def test_wait_for_conclusion_immediate_true_no_sleep(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_seen(store, rid="seen-1")
    _insert_conclusion(store, rid="fix-1", error_id="seen-1")
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert (
        _wait_for_conclusion(
            store,
            "seen-1",
            timeout_s=10.0,
            poll_interval_s=5.0,
            sleep=fake_sleep,
        )
        is True
    )
    assert sleeps == []


def test_wait_for_conclusion_timeout_calls_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    _insert_seen(store, rid="seen-1")
    sleeps: list[float] = []
    clock = {"t": 0.0}

    def fake_mono() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr("agent_cli.error_decide_act.time.monotonic", fake_mono)
    assert (
        _wait_for_conclusion(
            store,
            "seen-1",
            timeout_s=10.0,
            poll_interval_s=5.0,
            sleep=fake_sleep,
        )
        is False
    )
    assert sleeps == [5.0, 5.0]


def test_scan_error_decide_normal_path(tmp_path: Path) -> None:
    store = Store(tmp_path)
    error_id = "error-seen-aaaaaaaa"
    _insert_seen(store, rid=error_id, first_seen="2026-08-23T16:00:00Z")
    started: list[str] = []
    stopped: list[str] = []
    knocked: list[tuple[str, str]] = []

    def start(sid: str) -> None:
        started.append(sid)

    def stop(sid: str) -> None:
        stopped.append(sid)

    def knock(sid: str, eid: str) -> None:
        knocked.append((sid, eid))
        _insert_conclusion(store, rid="fix-1", error_id=eid, session_id=sid)

    lines = scan_error_decide(
        store,
        start=start,
        stop=stop,
        knock=knock,
        sleep=lambda _s: None,
        timeout_s=1.0,
        poll_interval_s=0.01,
    )
    sid = decide_session_id(error_id)
    assert started == [sid]
    assert stopped == [sid]
    assert knocked == [(sid, error_id)]
    assert lines == [f"error.seen {error_id} decided session={sid}"]
    session = store.row("session", sid)
    assert session is not None
    assert session["kind"] == "runner"
    assert session["skills"] == ["error-fix", "spine", "review-loop", "pr-review"]
    assert session["status"] == "active"


def test_scan_error_decide_timeout_still_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    error_id = "error-seen-bbbbbbbb"
    _insert_seen(store, rid=error_id)
    started: list[str] = []
    stopped: list[str] = []
    sleeps: list[float] = []
    clock = {"t": 0.0}

    def fake_mono() -> float:
        return clock["t"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr("agent_cli.error_decide_act.time.monotonic", fake_mono)
    lines = scan_error_decide(
        store,
        start=lambda sid: started.append(sid),
        stop=lambda sid: stopped.append(sid),
        knock=lambda _sid, _eid: None,
        sleep=fake_sleep,
        timeout_s=10.0,
        poll_interval_s=5.0,
    )
    sid = decide_session_id(error_id)
    assert started == [sid]
    assert stopped == [sid]
    assert sleeps == [5.0, 5.0]
    assert lines == [f"error.seen {error_id} timeout session={sid}"]


def test_scan_error_decide_processes_backlog_one_at_a_time(tmp_path: Path) -> None:
    store = Store(tmp_path)
    older = "aaa11111-seen-cccccccc"
    newer = "bbb22222-seen-dddddddd"
    _insert_seen(store, rid=newer, first_seen="2026-08-23T17:00:00Z")
    _insert_seen(store, rid=older, first_seen="2026-08-23T15:00:00Z")
    started: list[str] = []
    order: list[str] = []

    def knock(sid: str, eid: str) -> None:
        order.append(eid)
        _insert_conclusion(
            store,
            rid=f"fix-{eid}",
            error_id=eid,
            session_id=sid,
        )

    lines = scan_error_decide(
        store,
        start=lambda sid: started.append(sid),
        stop=lambda _sid: None,
        knock=knock,
        sleep=lambda _s: None,
        timeout_s=1.0,
        poll_interval_s=0.01,
    )
    assert order == [older, newer]
    assert started == [decide_session_id(older), decide_session_id(newer)]
    assert decide_session_id(older) != decide_session_id(newer)
    assert len(started) == 2
    assert lines == [
        f"error.seen {older} decided session={decide_session_id(older)}",
        f"error.seen {newer} decided session={decide_session_id(newer)}",
    ]


def test_scan_error_decide_skips_already_concluded_without_line(tmp_path: Path) -> None:
    store = Store(tmp_path)
    first = "error-seen-eeeeeeee"
    second = "error-seen-ffffffff"
    _insert_seen(store, rid=first, first_seen="2026-08-23T15:00:00Z")
    _insert_seen(store, rid=second, first_seen="2026-08-23T16:00:00Z")
    started: list[str] = []

    def knock(sid: str, eid: str) -> None:
        _insert_conclusion(store, rid=f"fix-{eid}", error_id=eid, session_id=sid)
        # Conclude the later backlog row while the first is being processed.
        if eid == first:
            _insert_conclusion(
                store,
                rid="fix-second-early",
                error_id=second,
                typ="error.skip",
                session_id=sid,
            )

    lines = scan_error_decide(
        store,
        start=lambda sid: started.append(sid),
        stop=lambda _sid: None,
        knock=knock,
        sleep=lambda _s: None,
        timeout_s=1.0,
        poll_interval_s=0.01,
    )
    assert started == [decide_session_id(first)]
    assert lines == [f"error.seen {first} decided session={decide_session_id(first)}"]


def test_scan_error_decide_stop_runs_when_knock_raises(tmp_path: Path) -> None:
    store = Store(tmp_path)
    error_id = "error-seen-gggggggg"
    _insert_seen(store, rid=error_id)
    started: list[str] = []
    stopped: list[str] = []

    def knock(_sid: str, _eid: str) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        scan_error_decide(
            store,
            start=lambda sid: started.append(sid),
            stop=lambda sid: stopped.append(sid),
            knock=knock,
            sleep=lambda _s: None,
            timeout_s=1.0,
            poll_interval_s=0.01,
        )
    sid = decide_session_id(error_id)
    assert started == [sid]
    assert stopped == [sid]


def test_ensure_decide_session_unions_missing_skills(tmp_path: Path) -> None:
    store = Store(tmp_path)
    error_id = "error-seen-hhhhhhhh"
    sid = decide_session_id(error_id)
    now = utcnow()
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "started_at": now,
            "last_seen_at": now,
            "host": socket.gethostname(),
            "status": "active",
            "skills": ["error-fix", "custom-extra"],
        },
    )
    _ensure_decide_session(store, sid, now)
    session = store.row("session", sid)
    assert session is not None
    skills = session.get("skills")
    assert skills == ["error-fix", "custom-extra", "spine", "review-loop", "pr-review"]


def test_ensure_decide_session_rejects_non_runner_kind(tmp_path: Path) -> None:
    store = Store(tmp_path)
    error_id = "error-seen-iiiiiiii"
    sid = decide_session_id(error_id)
    now = utcnow()
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "human",
            "started_at": now,
            "last_seen_at": now,
            "host": socket.gethostname(),
            "status": "active",
            "skills": ["error-fix", "spine", "review-loop", "pr-review"],
        },
    )
    with pytest.raises(StoreError, match="must be runner"):
        _ensure_decide_session(store, sid, now)


def test_scan_error_decide_lock_serializes_overlapping_scans(tmp_path: Path) -> None:
    # Two independent Store connections against the same tmp_path (same device
    # identity, same AGENT_PG_DSN test database) so the exclusive lock under test
    # is the real pg_advisory_lock, not just the in-process threading.RLock that
    # each Store instance also happens to hold internally.
    store_a = Store(tmp_path)
    store_b = Store(tmp_path)
    error_id = "error-seen-jjjjjjjj"
    _insert_seen(store_a, rid=error_id)

    barrier = threading.Barrier(2)
    started: list[str] = []
    started_lock = threading.Lock()
    errors: list[BaseException] = []

    def start(sid: str) -> None:
        with started_lock:
            started.append(sid)

    def make_knock(store: Store) -> Callable[[str, str], None]:
        def knock(sid: str, eid: str) -> None:
            # If the lock only covered the backlog read (the pre-fix bug), both
            # threads could reach this point concurrently and the barrier would
            # release both parties. With the lock held for the whole scan, only
            # one thread is ever here at a time, so the second party never shows
            # up and this always times out - that timeout is the proof of
            # serialization, not a test bug, so it is swallowed below.
            try:
                barrier.wait(timeout=0.3)
            except threading.BrokenBarrierError:
                pass
            _insert_conclusion(store, rid=f"fix-{sid}", error_id=eid, session_id=sid)

        return knock

    def run(store: Store) -> None:
        try:
            scan_error_decide(
                store,
                start=start,
                stop=lambda _sid: None,
                knock=make_knock(store),
                sleep=lambda _s: None,
                timeout_s=5.0,
                poll_interval_s=0.01,
            )
        except BaseException as exc:  # pragma: no cover - surfaced via assertion below
            errors.append(exc)

    t1 = threading.Thread(target=run, args=(store_a,))
    t2 = threading.Thread(target=run, args=(store_b,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert not errors
    assert started == [decide_session_id(error_id)]
