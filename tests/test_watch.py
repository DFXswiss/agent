from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from agent_cli.watch import (
    ISSUE_LIST_LIMIT,
    dispatch_assigned,
    load_watch_config,
    pending_assigned,
    scan_assigned as account_scan_assigned,
    scan_merged as account_scan_merged,
)



from github_support import configure_accounts, transport


def scan_assigned(store, runner, *, now):
    path = store.home / 'watch.json'
    try:
        sid = json.loads(path.read_text()).get('session_id', 'assigned')
    except (OSError, ValueError):
        sid = 'assigned'
    configure_accounts(store.home, [sid])
    return account_scan_assigned(store, transport(runner, authenticate=True), now=now)


def scan_merged(store, runner):
    configure_accounts(store.home, [r['id'] for r in store.rows('session')])
    return account_scan_merged(store, transport(runner))


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


def test_scan_merged_skips_rate_limited_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: 1_700_000_000)
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
    configure_accounts(store.home, ["s1"])

    def runner(argv: list[str]) -> Completed:
        command = argv[argv.index("gh"):]
        if "--include" in command:
            return Completed(
                1,
                "HTTP/2.0 403 Forbidden\nRetry-After: 45\n\n",
                "gh: You have exceeded a secondary rate limit. (HTTP 403)\n",
            )
        return Completed(1, "", "gh: You have exceeded a secondary rate limit. (HTTP 403)\n")

    created, skipped = account_scan_merged(store, runner)
    assert created == []
    assert skipped == 1
    stored = json.dumps(store.rows("activity"))
    assert "Retry-After" not in stored
    assert "HTTP/2.0" not in stored


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
    assert store.sync_get("assigned_session_id") == "assigned"


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


def test_scan_assigned_equal_cursor_inserts_once(tmp_path: Path) -> None:
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
    assert skipped == 0
    assert len(created) == 1
    again, skipped_again = scan_assigned(store, runner, now="2026-08-23T13:00:00Z")
    assert skipped_again == 0
    assert again == []


def test_scan_assigned_rejects_session_id_change(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    created, skipped = scan_assigned(
        store,
        lambda argv: Completed(0, json.dumps({"login": "alice"}), ""),
        now="2026-08-23T12:00:00Z",
    )
    assert created == []
    assert skipped == 0
    assert store.sync_get("assigned_session_id") == "assigned"
    (tmp_path / "watch.json").write_text(
        json.dumps({"assigned_repos": ["Owner/repo"], "session_id": "other"}),
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match="assigned worker is already assigned"):
        scan_assigned(
            store,
            lambda argv: Completed(0, json.dumps({"login": "alice"}), ""),
            now="2026-08-23T13:00:00Z",
        )


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
    assert store.sync_get("assigned_session_id") is None
    assert store.sync_get("assigned_watch_since") is None


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
    assert "mandate=github-assignment is trusted" in mandate
    assert "Do not ask whether to implement" in mandate
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


def test_pending_assigned_keeps_delivered_inflight_as_head(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.write(
        "session",
        "insert",
        "assigned",
        {"id": "assigned", "kind": "runner", "status": "active"},
    )
    store.write(
        "activity",
        "insert",
        "asg-new",
        {
            "id": "asg-new",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {"repo": "Owner/repo", "number": 9, "assigned_at": "2026-06-01T00:00:00Z"},
            "execution_status": "done",
        },
    )
    assert store.claim_wake("asg-new") is True
    store.write(
        "activity",
        "insert",
        "asg-old",
        {
            "id": "asg-old",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {"repo": "Owner/repo", "number": 8, "assigned_at": "2021-01-01T00:00:00Z"},
            "execution_status": "done",
        },
    )
    pending = pending_assigned(store, "assigned")
    assert [row["id"] for row in pending] == ["asg-new", "asg-old"]
    store.write(
        "activity",
        "insert",
        "ack-new",
        {
            "id": "ack-new",
            "session_id": "assigned",
            "type": "issue.assigned.ack",
            "payload": {"assigned_id": "asg-new"},
            "execution_status": "done",
        },
    )
    pending = pending_assigned(store, "assigned")
    assert [row["id"] for row in pending] == ["asg-old"]


def test_scan_assigned_reassignment_while_pending_enqueues(tmp_path: Path) -> None:
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
        "pending-1",
        {
            "id": "pending-1",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "assigned_at": "2021-01-01T00:00:00Z",
            },
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
    assert row["id"] != "pending-1"
    assert row["payload"]["assigned_at"] == "2026-06-01T00:00:00Z"


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


def _open_pr(row_id: str, number: int) -> dict:
    return {
        "id": row_id,
        "session_id": "s1",
        "type": "pr.open",
        "payload": {},
        "result": {
            "repo": "dfxswiss/agent",
            "number": number,
            "url": f"https://github.com/dfxswiss/agent/pull/{number}",
        },
        "execution_status": "done",
    }


def test_scan_merged_stops_the_same_account_until_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cli.coordinator_runtime import GITHUB_RATE_LIMIT_PREFIX

    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: 1_700_000_000)
    stamp = datetime.fromtimestamp(1_700_000_000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr("agent_cli.coordinator_runtime.utcnow", lambda: stamp)
    store = Store(tmp_path)
    store.write("session", "insert", "s1", {"id": "s1", "kind": "human", "status": "active"})
    store.write("activity", "insert", "open-1", _open_pr("open-1", 8))
    store.write("activity", "insert", "open-2", _open_pr("open-2", 9))
    configure_accounts(store.home, ["s1"])
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        command = argv[argv.index("gh") :]
        calls.append(command)
        if command[:3] != ["gh", "api", "user"]:
            raise AssertionError(command)
        if "--include" in command:
            return Completed(
                1,
                "HTTP/2.0 403 Forbidden\nRetry-After: 120\n\n",
                "gh: You have exceeded a secondary rate limit. (HTTP 403)\n",
            )
        return Completed(1, "", "gh: You have exceeded a secondary rate limit. (HTTP 403)\n")

    created, skipped = account_scan_merged(store, runner)
    assert created == []
    assert skipped == 2
    assert calls == [
        ["gh", "api", "user", "--jq", ".login"],
        ["gh", "api", "user", "--jq", ".login", "--include"],
    ]
    assert store.sync_get(GITHUB_RATE_LIMIT_PREFIX + "test") is not None
    stored = json.dumps(store.rows("activity"))
    assert "Retry-After" not in stored
    assert "HTTP/2.0" not in stored
    created, skipped = account_scan_merged(store, runner)
    assert created == []
    assert skipped == 2
    assert calls == [
        ["gh", "api", "user", "--jq", ".login"],
        ["gh", "api", "user", "--jq", ".login", "--include"],
    ]


def test_scan_assigned_rate_limited_login_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent_cli.github_accounts.time.time", lambda: 1_700_000_000)
    store = Store(tmp_path)
    _write_assigned_repos(tmp_path)
    configure_accounts(store.home, ["assigned"])
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        command = argv[argv.index("gh") :]
        calls.append(command)
        if "--include" in command:
            return Completed(
                1,
                "HTTP/2.0 403 Forbidden\nRetry-After: 45\n\n",
                "gh: You have exceeded a secondary rate limit. (HTTP 403)\n",
            )
        return Completed(1, "", "gh: You have exceeded a secondary rate limit. (HTTP 403)\n")

    created, skipped = account_scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 1
    assert all(command[:3] != ["gh", "issue", "list"] for command in calls)
    stored = json.dumps(store.rows("activity") + [store.sync_get("assigned_watch_since")])
    assert "Retry-After" not in stored
    assert "HTTP/2.0" not in stored


def test_scan_assigned_rate_limit_does_not_read_the_next_repo(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _write_assigned_repos(tmp_path, ["Owner/one", "Owner/two"])
    configure_accounts(store.home, ["assigned"])
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        command = argv[argv.index("gh") :]
        calls.append(command)
        if command[:3] == ["gh", "api", "user"]:
            return Completed(0, "alice\n", "")
        if command[:3] == ["gh", "issue", "list"]:
            return Completed(
                1,
                "",
                "gh: You have exceeded a secondary rate limit. (HTTP 403)\nretry-after: 60\n",
            )
        raise AssertionError(command)

    created, skipped = account_scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 2
    listed = [command for command in calls if command[:3] == ["gh", "issue", "list"]]
    assert len(listed) == 1
    assert listed[0][listed[0].index("--repo") + 1] == "Owner/one"


def test_scan_assigned_rate_limited_events_skip_the_line(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _write_assigned_repos(tmp_path)
    configure_accounts(store.home, ["assigned"])
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        command = argv[argv.index("gh") :]
        calls.append(command)
        if command[:3] == ["gh", "api", "user"]:
            return Completed(0, "alice\n", "")
        if command[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {"number": 1, "title": "One", "url": "https://example/1", "body": ""},
                        {"number": 2, "title": "Two", "url": "https://example/2", "body": ""},
                    ]
                ),
                "",
            )
        if command[:2] == ["gh", "api"] and command[-1].endswith("/events"):
            return Completed(
                1,
                "",
                "gh: You have exceeded a secondary rate limit. (HTTP 403)\nretry-after: 60\n",
            )
        raise AssertionError(command)

    created, skipped = account_scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 2
    events = [command for command in calls if command[-1].endswith("/events")]
    assert len(events) == 1
    assert events[0][-1].endswith("/issues/1/events")
    stored = json.dumps(store.rows("activity"))
    assert "retry-after" not in stored


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
