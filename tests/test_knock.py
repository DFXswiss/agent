from __future__ import annotations

from pathlib import Path

from agent_cli.knock import deliver, drain, knock_text, listen_once
from agent_cli.runtime import Completed, Runtime
from agent_cli.store import Store


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
