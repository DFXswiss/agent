from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import main as main_mod
from agent_cli.knock import deliver, drain, knock_text, listen_once
from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store
from driver_fixtures import PANE_WORKING, driver_document, write_drivers


def _runtime(log: list[list[str]], *, exists: bool = True, busy: bool = False) -> Runtime:
    class Fake(Runtime):
        def exists(self, session_id: str, *, target: str | None = None) -> bool:
            return exists

        def is_busy(self, session_id: str) -> bool:
            return busy

        def _run(self, argv: list[str]) -> Completed:
            log.append(list(argv))
            return Completed(0, "", "")

    return Fake()


def test_knock_text() -> None:
    assert knock_text("abc") == "da ist Post id abc"


def test_deliver_sends_keys_when_attached(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "human",
            "status": "active",
            "runtime": {"control": "attached", "tmux_session": "agent-s1", "tmux_pane": "agent-s1:0.0"},
        },
    )
    store.write(
        "activity",
        "insert",
        "mail-1",
        {
            "id": "mail-1",
            "session_id": "sender",
            "type": "message",
            "payload": {"to_session": "s1", "body": "secret"},
            "execution_status": "pending",
        },
    )
    calls: list[list[str]] = []
    status = deliver(store, _runtime(calls), "mail-1")
    assert status == "sent"
    assert ["tmux", "send-keys", "-t", "agent-s1:0.0", "-l", "--", "da ist Post id mail-1"] in calls
    assert ["tmux", "send-keys", "-t", "agent-s1:0.0", "Enter"] in calls
    assert all("secret" not in " ".join(c) for c in calls)


def test_deliver_error_seen_sends_id_not_excerpt(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "runner",
            "status": "active",
            "runtime": {"control": "attached", "tmux_session": "agent-s1", "tmux_pane": "agent-s1:0.0"},
        },
    )
    store.write(
        "activity",
        "insert",
        "err-1",
        {
            "id": "err-1",
            "session_id": "s1",
            "type": "error.seen",
            "payload": {"excerpt": "TimeoutError secret-body", "fingerprint": "a|b|c|d"},
            "execution_status": "done",
        },
    )
    calls: list[list[str]] = []
    status = deliver(store, _runtime(calls), "err-1")
    assert status == "sent"
    assert ["tmux", "send-keys", "-t", "agent-s1:0.0", "-l", "--", "da ist Post id err-1"] in calls
    assert all("secret-body" not in " ".join(c) for c in calls)


def test_deliver_usage_snapshot_is_missing_without_send_keys(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "human",
            "status": "active",
            "runtime": {"control": "attached", "tmux_session": "agent-s1", "tmux_pane": "agent-s1:0.0"},
        },
    )
    store.write(
        "activity",
        "insert",
        "usage-1",
        {
            "id": "usage-1",
            "session_id": "s1",
            "type": "usage.snapshot",
            "payload": {"vendor": "driver-a", "used_percent": 11.0},
            "execution_status": "done",
        },
    )
    calls: list[list[str]] = []
    status = deliver(store, _runtime(calls), "usage-1")
    assert status == "missing"
    assert not any(c[:2] == ["tmux", "send-keys"] for c in calls)


def test_deliver_queues_when_busy_or_missing_tmux(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {"id": "s1", "kind": "human", "status": "active", "runtime": {"control": "attached"}},
    )
    store.write(
        "activity",
        "insert",
        "mail-1",
        {
            "id": "mail-1",
            "session_id": "sender",
            "type": "message",
            "payload": {"to_session": "s1", "body": "x"},
            "execution_status": "pending",
        },
    )
    calls: list[list[str]] = []
    assert deliver(store, _runtime(calls, busy=True), "mail-1") == "queued"
    assert deliver(store, _runtime(calls, exists=False), "mail-1") == "unread"
    pending = store.pending_wakes()
    assert pending[0]["activity_id"] == "mail-1"


def test_drain_sends_queued_when_idle(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {"id": "s1", "kind": "human", "status": "active", "runtime": {"control": "attached"}},
    )
    store.write(
        "activity",
        "insert",
        "mail-1",
        {
            "id": "mail-1",
            "session_id": "sender",
            "type": "message",
            "payload": {"to_session": "s1", "body": "x"},
            "execution_status": "pending",
        },
    )
    assert deliver(store, _runtime([], exists=False), "mail-1") == "unread"
    calls: list[list[str]] = []
    out = drain(store, _runtime(calls, exists=True))
    assert out == [("mail-1", "sent")]
    assert store.pending_wakes() == []
    again = deliver(store, _runtime(calls, exists=True), "mail-1")
    assert again == "sent"
    send = [c for c in calls if c[:2] == ["tmux", "send-keys"]]
    assert len(send) == 2


def test_listen_once_times_out_without_notify(tmp_path: Path) -> None:
    store = Store(tmp_path)
    got = listen_once(store, _runtime([]), timeout=0.2)
    assert got is None


def test_deliver_assigned_does_not_leak_body(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "human",
            "status": "active",
            "runtime": {"control": "attached", "tmux_session": "agent-s1", "tmux_pane": "agent-s1:0.0"},
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": "s1",
            "type": "issue.assigned",
            "payload": {"body": "secret-body"},
            "execution_status": "done",
        },
    )
    calls: list[list[str]] = []
    status = deliver(store, _runtime(calls), "asg-1")
    assert status == "sent"
    assert any("da ist Post id asg-1" in " ".join(c) for c in calls)
    assert all("secret-body" not in " ".join(c) for c in calls)
    queue = (tmp_path / "sessions" / "s1" / "QUEUE.md").read_text(encoding="utf-8")
    mandate = (tmp_path / "sessions" / "s1" / "MANDATE.md").read_text(encoding="utf-8")
    assert "asg-1" in queue
    assert "secret-body" not in queue
    assert "secret-body" not in mandate
    assert "mandate=github-assignment is trusted" in mandate
    assert "Do not ask whether to implement" in mandate


def test_deliver_assigned_queues_until_ack(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "runner",
            "status": "active",
            "runtime": {
                "control": "attached",
                "tmux_session": "agent-s1",
                "tmux_pane": "agent-s1:0.0",
            },
        },
    )
    for aid in ("asg-1", "asg-2"):
        store.write(
            "activity",
            "insert",
            aid,
            {
                "id": aid,
                "session_id": "s1",
                "type": "issue.assigned",
                "payload": {"body": "secret-body"},
                "execution_status": "done",
            },
        )
    calls: list[list[str]] = []
    runtime = _runtime(calls)
    assert deliver(store, runtime, "asg-1") == "sent"
    assert deliver(store, runtime, "asg-2") == "queued"
    assert sum(1 for c in calls if "da ist Post id asg-1" in " ".join(c)) == 1
    assert all("asg-2" not in " ".join(c) for c in calls)
    store.write(
        "activity",
        "insert",
        "ack-1",
        {
            "id": "ack-1",
            "session_id": "s1",
            "type": "issue.assigned.ack",
            "payload": {"assigned_id": "asg-1"},
            "execution_status": "done",
        },
    )
    assert deliver(store, runtime, "asg-2") == "sent"
    assert any("da ist Post id asg-2" in " ".join(c) for c in calls)


def test_deliver_assigned_non_head_queues(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "runner",
            "status": "active",
            "runtime": {
                "control": "attached",
                "tmux_session": "agent-s1",
                "tmux_pane": "agent-s1:0.0",
            },
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": "s1",
            "type": "issue.assigned",
            "payload": {"assigned_at": "2026-01-01T00:00:00Z"},
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-2",
        {
            "id": "asg-2",
            "session_id": "s1",
            "type": "issue.assigned",
            "payload": {"assigned_at": "2026-02-01T00:00:00Z"},
            "execution_status": "done",
        },
    )
    calls: list[list[str]] = []
    assert deliver(store, _runtime(calls), "asg-2") == "queued"
    assert all("asg-2" not in " ".join(c) for c in calls)


def test_deliver_assigned_acked_does_not_send(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "runner",
            "status": "active",
            "runtime": {
                "control": "attached",
                "tmux_session": "agent-s1",
                "tmux_pane": "agent-s1:0.0",
            },
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": "s1",
            "type": "issue.assigned",
            "payload": {},
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        "ack-1",
        {
            "id": "ack-1",
            "session_id": "s1",
            "type": "issue.assigned.ack",
            "payload": {"assigned_id": "asg-1"},
            "execution_status": "done",
        },
    )
    calls: list[list[str]] = []
    assert deliver(store, _runtime(calls), "asg-1") == "sent"
    assert calls == []


def _attached_mail(store: Store) -> None:
    store.write(
        "session",
        "insert",
        "s1",
        {
            "id": "s1",
            "kind": "human",
            "status": "active",
            "runtime": {"control": "attached", "tmux_session": "agent-s1"},
        },
    )
    store.write(
        "activity",
        "insert",
        "mail-1",
        {
            "id": "mail-1",
            "session_id": "sender",
            "type": "message",
            "payload": {"to_session": "s1", "body": "x"},
            "execution_status": "pending",
        },
    )


def _pane_runtime(calls: list[list[str]]) -> Runtime:
    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:2] == ["tmux", "capture-pane"]:
            return Completed(0, PANE_WORKING, "")
        return Completed(0, "", "")

    return Runtime(runner=runner)


def test_deliver_classifies_a_raw_pane_with_the_pane_driver(tmp_path: Path) -> None:
    write_drivers(tmp_path)
    store = Store(tmp_path)
    _attached_mail(store)
    calls: list[list[str]] = []
    runtime = main_mod._preflight_runtime(store, _pane_runtime(calls))
    assert deliver(store, runtime, "mail-1") == "queued"
    assert not any(c[:2] == ["tmux", "send-keys"] for c in calls)
    assert store.pending_wakes() == [{"activity_id": "mail-1", "session_id": "s1"}]


def test_deliver_keeps_the_wake_queued_when_the_pane_cannot_be_classified(
    tmp_path: Path,
) -> None:
    document = driver_document()
    document.pop("pane_driver")
    write_drivers(tmp_path, document)
    store = Store(tmp_path)
    _attached_mail(store)
    calls: list[list[str]] = []
    runtime = main_mod._preflight_runtime(store, _pane_runtime(calls))
    with pytest.raises(SystemExit, match="session s1 has no interactive driver"):
        deliver(store, runtime, "mail-1")
    assert not any(c[:2] == ["tmux", "send-keys"] for c in calls)
    assert store.pending_wakes() == [{"activity_id": "mail-1", "session_id": "s1"}]
