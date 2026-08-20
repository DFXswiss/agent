from __future__ import annotations

import json
from pathlib import Path

from agent_cli.runtime import Completed
from agent_cli.store import Store
from agent_cli.watch import scan_merged


def test_scan_merged_inserts_once(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})
    store.write(
        "activity",
        "insert",
        "open-1",
        {
            "id": "open-1",
            "session_id": "s1",
            "type": "pr.open",
            "payload": {"repo": "dfxswiss/agent", "number": 8},
            "result": {"repo": "dfxswiss/agent", "number": 8, "url": "https://github.com/dfxswiss/agent/pull/8"},
            "execution_status": "done",
        },
    )

    def runner(argv: list[str]) -> Completed:
        assert argv[:3] == ["gh", "pr", "view"]
        body = {
            "state": "MERGED",
            "mergedAt": "2026-08-13T12:00:00Z",
            "mergeCommit": {"oid": "abc1234"},
            "url": "https://github.com/dfxswiss/agent/pull/8",
            "number": 8,
        }
        return Completed(0, json.dumps(body), "")

    created, skipped = scan_merged(store, runner)
    assert skipped == 0
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["type"] == "pr.merged"
    assert row["session_id"] == "s1"
    assert row["payload"]["number"] == 8
    assert row["payload"]["merge_sha"] == "abc1234"
    again, skipped_again = scan_merged(store, runner)
    assert skipped_again == 0
    assert again == []


def test_scan_merged_skips_gh_failure(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})
    store.write(
        "activity",
        "insert",
        "open-1",
        {
            "id": "open-1",
            "session_id": "s1",
            "type": "pr.open",
            "payload": {},
            "result": {"repo": "dfxswiss/agent", "number": 1, "url": "https://github.com/dfxswiss/agent/pull/1"},
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        "open-2",
        {
            "id": "open-2",
            "session_id": "s1",
            "type": "pr.open",
            "payload": {},
            "result": {"repo": "dfxswiss/agent", "number": 8, "url": "https://github.com/dfxswiss/agent/pull/8"},
            "execution_status": "done",
        },
    )

    def runner(argv: list[str]) -> Completed:
        if argv[3] == "1":
            return Completed(1, "", "not found")
        body = {
            "state": "MERGED",
            "mergedAt": "2026-08-13T12:00:00Z",
            "mergeCommit": {"oid": "abc1234"},
            "url": "https://github.com/dfxswiss/agent/pull/8",
            "number": 8,
        }
        return Completed(0, json.dumps(body), "")

    created, skipped = scan_merged(store, runner)
    assert skipped == 1
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["payload"]["number"] == 8
