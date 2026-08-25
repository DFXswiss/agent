from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_cli.mail_act import scan_mail, scan_mail_ingest
from agent_cli.runtime import Completed
from agent_cli.store import Store


def _owned_session(store: Store, sid: str = "s1") -> None:
    store.write("session", "insert", sid, {"id": sid, "kind": "human", "status": "active"})


def _pending(
    store: Store,
    act_id: str,
    typ: str,
    payload: dict[str, Any],
    *,
    session_id: str = "s1",
) -> None:
    store.write(
        "activity",
        "insert",
        act_id,
        {
            "id": act_id,
            "session_id": session_id,
            "type": typ,
            "payload": payload,
            "execution_status": "pending",
        },
    )


def test_mail_reply_success(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "reply-1"
    _pending(
        store,
        act_id,
        "mail.reply",
        {
            "to": "peer",
            "body": "thanks",
            "subject": "Re: hello",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_mail(store, runner)
    assert lines == [f"mail.reply {act_id} done"]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"] == {"to": "peer"}
    assert calls == [
        [
            "himalaya",
            "message",
            "compose",
            "--to",
            "peer",
            "--subject",
            "Re: hello",
            "--body",
            "thanks",
            "--send",
        ]
    ]


def test_mail_reply_in_reply_to_without_to(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "reply-2"
    _pending(store, act_id, "mail.reply", {"in_reply_to": 11, "body": "thanks"})
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_mail(store, runner)
    assert lines == [f"mail.reply {act_id} done"]
    assert calls == [
        ["himalaya", "message", "reply", "11", "--body", "thanks", "--send"]
    ]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"] == {"in_reply_to": "11"}


def test_mail_reply_missing_to_no_runner(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "reply-bad"
    _pending(store, act_id, "mail.reply", {"body": "x"})
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_mail(store, runner)
    assert lines == [f"mail.reply {act_id} error"]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert calls == []


def test_mail_seen_default_inbox(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "seen-1"
    _pending(store, act_id, "mail.seen", {"id": 42})
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "", "")

    lines = scan_mail(store, runner)
    assert lines == [f"mail.seen {act_id} done"]
    assert calls == [
        ["himalaya", "flag", "add", "-m", "Inbox", "--flag", "seen", "42"]
    ]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"


def test_mail_ingest_inserts_and_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    envelopes = [
        {
            "id": 7,
            "from": "sender",
            "subject": "Hello",
            "date": "2026-01-01T00:00:00Z",
        }
    ]
    calls = 0

    def runner(argv: list[str]) -> Completed:
        nonlocal calls
        calls += 1
        assert argv == ["himalaya", "--json", "envelope", "list", "--page-size", "30"]
        return Completed(0, json.dumps(envelopes), "")

    lines1 = scan_mail_ingest(store, runner)
    assert len(lines1) == 1
    assert lines1[0].startswith("mail.ingest ")
    assert lines1[0].endswith(" done")
    rows = [r for r in store.rows("activity") if r.get("type") == "mail.ingest"]
    assert len(rows) == 1
    assert rows[0]["execution_status"] == "done"
    assert rows[0]["payload"]["id"] == 7
    assert rows[0]["payload"]["from"] == "sender"
    assert rows[0]["payload"]["subject"] == "Hello"
    assert rows[0]["session_id"] == "s1"

    lines2 = scan_mail_ingest(store, runner)
    assert lines2 == []
    assert calls == 2
    assert len([r for r in store.rows("activity") if r.get("type") == "mail.ingest"]) == 1
