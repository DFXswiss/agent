from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from agent_cli.watch import (
    ISSUE_LIST_LIMIT,
    dispatch_assigned,
    load_watch_config,
    scan_assigned,
    scan_merged,
)


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


def test_scan_merged_counts_merged_without_sha(tmp_path: Path) -> None:
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
            "result": {"repo": "dfxswiss/agent", "number": 8, "url": "https://github.com/dfxswiss/agent/pull/8"},
            "execution_status": "done",
        },
    )

    def runner(argv: list[str]) -> Completed:
        body = {
            "state": "MERGED",
            "mergedAt": "2026-08-13T12:00:00Z",
            "mergeCommit": None,
            "url": "https://github.com/dfxswiss/agent/pull/8",
            "number": 8,
        }
        return Completed(0, json.dumps(body), "")

    created, skipped = scan_merged(store, runner)
    assert created == []
    assert skipped == 1


def test_scan_merged_counts_missing_gh(tmp_path: Path) -> None:
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
            "result": {"repo": "dfxswiss/agent", "number": 8, "url": "https://github.com/dfxswiss/agent/pull/8"},
            "execution_status": "done",
        },
    )

    def runner(argv: list[str]) -> Completed:
        raise FileNotFoundError("gh")

    created, skipped = scan_merged(store, runner)
    assert created == []
    assert skipped == 1


def _write_assigned_repos(home: Path, repos: list[str] | None = None) -> None:
    path = home / "watch.json"
    if repos is None:
        repos = ["Owner/repo"]
    path.write_text(json.dumps({"assigned_repos": repos}), encoding="utf-8")


def test_assigned_repos_missing_file(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")

    def runner(argv: list[str]) -> Completed:
        return Completed(0, json.dumps({"login": "alice"}), "")

    with pytest.raises(StoreError, match="assigned_repos|watch.json"):
        scan_assigned(store, runner, now="2026-08-23T12:00:00Z")


def test_assigned_repos_rejects_non_owner_repo(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    (tmp_path / "watch.json").write_text(
        json.dumps({"assigned_repos": ["Owner"]}),
        encoding="utf-8",
    )

    def runner(argv: list[str]) -> Completed:
        return Completed(0, json.dumps({"login": "alice"}), "")

    with pytest.raises(StoreError, match="Owner/repo"):
        scan_assigned(store, runner, now="2026-08-23T12:00:00Z")


def test_assigned_repos_empty_list(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path, [])

    def runner(argv: list[str]) -> Completed:
        return Completed(0, json.dumps({"login": "alice"}), "")

    with pytest.raises(StoreError, match="assigned_repos"):
        scan_assigned(store, runner, now="2026-08-23T12:00:00Z")


def test_scan_assigned_first_scan_sets_cursor_only(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            raise AssertionError("issue list must not run on first scan")
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 0
    assert not any(r.get("type") == "issue.assigned" for r in store.rows("activity"))
    assert store.sync_get("assigned_watch_since") == "2026-08-23T12:00:00Z"


def test_scan_assigned_truncated_issue_list_inserts_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps([{"number": i + 1} for i in range(ISSUE_LIST_LIMIT)]),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 1
    assert store.sync_get("assigned_watch_since") == "2020-01-01T00:00:00Z"
    assert not any(r.get("type") == "issue.assigned" for r in store.rows("activity"))


def test_scan_assigned_inserts_after_cursor_once(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Fix it",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any("events" in part for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert skipped == 0
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["type"] == "issue.assigned"
    assert row["payload"]["number"] == 8
    assert row["payload"]["mandate"] == "github-assignment"
    assert row["session_id"] == "assigned"
    session = store.row("session", row["session_id"])
    assert session is not None
    assert session["kind"] == "runner"
    assert set(session["skills"]) >= {"spine", "review-loop", "pr-review"}
    again, skipped_again = scan_assigned(store, runner, now="2026-08-23T13:00:00Z")
    assert skipped_again == 0
    assert again == []


def test_scan_assigned_does_not_mutate_existing_runner_skills(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    store.write(
        "session",
        "insert",
        "assigned",
        {
            "id": "assigned",
            "kind": "runner",
            "status": "active",
            "skills": ["spine"],
        },
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Fix it",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "",
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any("events" in part for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert skipped == 0
    assert len(created) == 1
    session = store.row("session", "assigned")
    assert session is not None
    assert session["skills"] == ["spine"]


def test_scan_assigned_equal_cursor_skips(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2026-01-01T00:00:00Z")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Fix it",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any("events" in part for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 0
    assert not any(r.get("type") == "issue.assigned" for r in store.rows("activity"))


def test_scan_assigned_gh_failure_skips_other_inserts(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    before = store.sync_get("assigned_watch_since")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "One",
                            "url": "https://github.com/Owner/repo/issues/1",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        },
                        {
                            "number": 8,
                            "title": "Eight",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        },
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any(part.endswith("/issues/1/events") for part in argv):
            return Completed(1, "", "boom")
        if argv[:2] == ["gh", "api"] and any(part.endswith("/issues/8/events") for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert skipped == 1
    assert created == []
    assert store.sync_get("assigned_watch_since") == before


def test_scan_assigned_login_mismatch(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "bob"}), "")
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(StoreError, match="does not match"):
        scan_assigned(store, runner, now="2026-08-23T12:00:00Z")


def test_dispatch_assigned_sync_failure_does_not_start(tmp_path: Path) -> None:
    store = Store(tmp_path)
    sid = "assigned"
    store.write(
        "session",
        "insert",
        sid,
        {"id": sid, "kind": "runner", "status": "active"},
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": sid,
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "url": "https://github.com/Owner/repo/issues/8",
                "title": "t",
                "body": "SECRET_BODY_DO_NOT_COPY",
                "mandate": "github-assignment",
            },
            "execution_status": "done",
        },
    )
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []

    def sync() -> None:
        raise StoreError("hub down")

    with pytest.raises(StoreError, match="hub down"):
        dispatch_assigned(
            store,
            "asg-1",
            sync=sync,
            start=lambda s, cwd: start_log.append((s, cwd)),
            knock=lambda aid: knock_log.append(aid),
            workspace_root=tmp_path / "sessions",
        )
    assert start_log == []
    assert knock_log == []


def test_dispatch_assigned_writes_mandate_and_starts(tmp_path: Path) -> None:
    store = Store(tmp_path)
    sid = "assigned"
    store.write(
        "session",
        "insert",
        sid,
        {"id": sid, "kind": "runner", "status": "active"},
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": sid,
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "url": "https://github.com/Owner/repo/issues/8",
                "title": "t",
                "body": "SECRET_BODY_DO_NOT_COPY",
                "mandate": "github-assignment",
            },
            "execution_status": "done",
        },
    )
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    workspace_root = tmp_path / "sessions"
    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
    )
    assert status == "started"
    assert start_log == [(sid, workspace_root / sid)]
    assert knock_log == ["asg-1"]
    mandate = (workspace_root / sid / "MANDATE.md").read_text(encoding="utf-8")
    assert "asg-1" in mandate
    assert "GitHub assignment is the work order" in mandate
    assert "one assignment worker" in mandate
    assert "SECRET_BODY_DO_NOT_COPY" not in mandate
    queue = (workspace_root / sid / "QUEUE.md").read_text(encoding="utf-8")
    assert "asg-1" in queue
    assert "SECRET_BODY_DO_NOT_COPY" not in queue


def test_dispatch_assigned_kicks_when_attached(tmp_path: Path) -> None:
    store = Store(tmp_path)
    sid = "assigned"
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "status": "active",
            "runtime": {"control": "attached"},
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": sid,
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "body": "SECRET_BODY_DO_NOT_COPY",
            },
            "execution_status": "done",
        },
    )
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    sync_log: list[int] = []
    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: sync_log.append(1),
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=tmp_path / "sessions",
    )
    assert status == "kicked"
    assert sync_log == [1]
    assert start_log == []
    assert knock_log == ["asg-1"]


def test_scan_assigned_two_issues_share_one_session(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 1,
                            "title": "One",
                            "url": "https://github.com/Owner/repo/issues/1",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        },
                        {
                            "number": 8,
                            "title": "Eight",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        },
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any(part.endswith("/issues/1/events") for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-02-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any(part.endswith("/issues/8/events") for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert skipped == 0
    assert len(created) == 2
    sessions = {store.row("activity", aid)["session_id"] for aid in created}
    assert sessions == {"assigned"}
    assert len([r for r in store.rows("session") if r.get("id") == "assigned"]) == 1
    first = store.row("activity", created[0])
    assert first is not None
    assert first["payload"]["number"] == 8


def test_dispatch_assigned_second_does_not_start_another_terminal(tmp_path: Path) -> None:
    store = Store(tmp_path)
    sid = "assigned"
    store.write(
        "session",
        "insert",
        sid,
        {"id": sid, "kind": "runner", "status": "active"},
    )
    for aid, number in (("asg-1", 1), ("asg-2", 8)):
        store.write(
            "activity",
            "insert",
            aid,
            {
                "id": aid,
                "session_id": sid,
                "type": "issue.assigned",
                "payload": {
                    "repo": "Owner/repo",
                    "number": number,
                    "assigned_at": f"2026-01-0{number}T00:00:00Z",
                    "body": "SECRET_BODY_DO_NOT_COPY",
                },
                "execution_status": "done",
            },
        )
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    workspace_root = tmp_path / "sessions"

    def start(session_id: str, cwd: Path) -> None:
        start_log.append((session_id, cwd))
        row = store.row("session", session_id)
        assert row is not None
        row["runtime"] = {"control": "attached"}
        store.write("session", "update", session_id, {k: v for k, v in row.items() if not k.startswith("_")})

    first = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=start,
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
    )
    second = dispatch_assigned(
        store,
        "asg-2",
        sync=lambda: None,
        start=start,
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
    )
    assert first == "started"
    assert second == "kicked"
    assert start_log == [(sid, workspace_root / sid)]
    assert knock_log == ["asg-1", "asg-1"]


def test_scan_assigned_after_ack_allows_new_assignment(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    store.write(
        "session",
        "insert",
        "assigned",
        {"id": "assigned", "kind": "runner", "status": "active"},
    )
    store.write(
        "activity",
        "insert",
        "old-1",
        {
            "id": "old-1",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {"repo": "Owner/repo", "number": 8, "assigned_at": "2021-01-01T00:00:00Z"},
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        "ack-old",
        {
            "id": "ack-old",
            "session_id": "assigned",
            "type": "issue.assigned.ack",
            "payload": {"assigned_id": "old-1"},
            "execution_status": "done",
        },
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Again",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "SECRET_BODY_DO_NOT_COPY",
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any("events" in part for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-06-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert skipped == 0
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["payload"]["number"] == 8
    assert row["id"] != "old-1"


def test_scan_assigned_does_not_requeue_same_assignment_after_ack(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    store.write(
        "session",
        "insert",
        "assigned",
        {"id": "assigned", "kind": "runner", "status": "active"},
    )
    store.write(
        "activity",
        "insert",
        "old-1",
        {
            "id": "old-1",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "assigned_at": "2026-06-01T00:00:00Z",
            },
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        "ack-old",
        {
            "id": "ack-old",
            "session_id": "assigned",
            "type": "issue.assigned.ack",
            "payload": {"assigned_id": "old-1"},
            "execution_status": "done",
        },
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Again",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "",
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any("events" in part for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-06-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert skipped == 0
    assert created == []


def test_load_watch_config_rejects_invalid_session_id(tmp_path: Path) -> None:
    (tmp_path / "watch.json").write_text(
        json.dumps({"assigned_repos": ["Owner/repo"], "session_id": "not/ok"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="session_id"):
        load_watch_config(tmp_path)


def test_scan_assigned_rejects_non_runner_session(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    store.write(
        "session",
        "insert",
        "assigned",
        {"id": "assigned", "kind": "human", "status": "active"},
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "api", "user"]:
            return Completed(0, json.dumps({"login": "alice"}), "")
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 8,
                            "title": "Fix it",
                            "url": "https://github.com/Owner/repo/issues/8",
                            "body": "",
                        }
                    ]
                ),
                "",
            )
        if argv[:2] == ["gh", "api"] and any("events" in part for part in argv):
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    with pytest.raises(StoreError, match="runner"):
        scan_assigned(store, runner, now="2026-08-23T12:00:00Z")


def test_dispatch_assigned_restarts_when_pane_down(tmp_path: Path) -> None:
    store = Store(tmp_path)
    sid = "assigned"
    store.write(
        "session",
        "insert",
        sid,
        {
            "id": sid,
            "kind": "runner",
            "status": "active",
            "runtime": {"control": "attached"},
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-1",
        {
            "id": "asg-1",
            "session_id": sid,
            "type": "issue.assigned",
            "payload": {"repo": "Owner/repo", "number": 8},
            "execution_status": "done",
        },
    )
    start_log: list[tuple[str, Path]] = []
    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: None,
        workspace_root=tmp_path / "sessions",
        pane_up=lambda _sid: False,
    )
    assert status == "started"
    assert start_log == [(sid, tmp_path / "sessions" / sid)]
