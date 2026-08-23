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
            "payload": {"vendor": "grok", "used_percent": 11.0},
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
