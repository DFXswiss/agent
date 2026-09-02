from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreError
from agent_cli.watch import (
    ISSUE_LIST_LIMIT,
    dispatch_assigned,
    load_policy,
    load_watch_config,
    pending_assigned,
    policy_present,
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
                            "actor": {"login": "bob"},
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
    assert row["payload"]["assigned_by"] == "bob"
    assert row["session_id"] == "assigned"
    session = store.row("session", row["session_id"])
    assert session is not None
    assert session["kind"] == "runner"
    assert set(session["skills"]) >= {"spine", "review-loop", "pr-review"}
    again, skipped_again = scan_assigned(store, runner, now="2026-08-23T13:00:00Z")
    assert skipped_again == 0
    assert again == []


def test_scan_assigned_missing_actor_skips_without_persisting(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    _write_admit_policy(tmp_path)
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
    assert created == []
    assert skipped == 0


def test_scan_assigned_missing_actor_still_persists_without_a_policy(
    tmp_path: Path,
) -> None:
    # Same fixture as test_scan_assigned_missing_actor_skips_without_persisting,
    # minus the policy.json: without an active policy there is nothing to jam,
    # so this must keep the pre-policy behavior of enqueueing what GitHub
    # reported as assigned, even with a blank assigned_by.
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
    row = store.row("activity", created[0])
    assert row is not None
    assert row["payload"]["assigned_by"] == ""


def test_scan_assigned_same_second_uses_higher_event_id(tmp_path: Path) -> None:
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
                            "id": 100,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "first"},
                        },
                        {
                            "id": 200,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "later"},
                        },
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
    assert row["payload"]["assigned_by"] == "later"
    assert row["payload"]["event_id"] == 200


def test_scan_assigned_same_second_unresolvable_tie_skips_without_persisting(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    _write_admit_policy(tmp_path)
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
                            "id": 100,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "first"},
                        },
                        {
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "middle"},
                        },
                        {
                            "id": 200,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "later"},
                        },
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T12:00:00Z")
    assert created == []
    assert skipped == 0


def test_scan_assigned_resolvable_candidate_beats_a_blanked_stored_marker(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    # Legacy stored marker with no event_id (field added later): scan_assigned
    # no longer manufactures these, but a resolvable candidate at the same
    # timestamp must still beat a stored marker with no id.
    store.write(
        "session",
        "insert",
        "assigned",
        {
            "id": "assigned",
            "kind": "runner",
            "status": "active",
            "started_at": "2026-01-01T00:00:00Z",
            "last_seen_at": "2026-01-01T00:00:00Z",
            "host": "test",
        },
    )
    store.sync_set("assigned_session_id", "assigned")
    store.write(
        "activity",
        "insert",
        "legacy-blanked",
        {
            "id": "legacy-blanked",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "url": "https://github.com/Owner/repo/issues/8",
                "title": "Fix it",
                "body": "SECRET_BODY_DO_NOT_COPY",
                "assigned_at": "2026-01-01T00:00:00Z",
                "assigned_by": "",
                "event_id": None,
                "mandate": "github-assignment",
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
                            "id": 100,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "first"},
                        },
                        {
                            "id": 300,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "resolved"},
                        },
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-08-23T13:00:00Z")
    assert skipped == 0
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["id"] != "legacy-blanked"
    assert row["payload"]["assigned_by"] == "resolved"
    assert row["payload"]["event_id"] == 300


def test_scan_assigned_same_second_higher_event_id_across_scans(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.set_meta("github_login", "alice")
    _write_assigned_repos(tmp_path)
    store.sync_set("assigned_watch_since", "2020-01-01T00:00:00Z")
    events_calls = {"n": 0}

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
            events_calls["n"] += 1
            if events_calls["n"] == 1:
                return Completed(
                    0,
                    json.dumps(
                        [
                            {
                                "id": 100,
                                "event": "assigned",
                                "created_at": "2026-01-01T00:00:00Z",
                                "assignee": {"login": "alice"},
                                "actor": {"login": "A"},
                            }
                        ]
                    ),
                    "",
                )
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "id": 100,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "A"},
                        },
                        {
                            "id": 200,
                            "event": "assigned",
                            "created_at": "2026-01-01T00:00:00Z",
                            "assignee": {"login": "alice"},
                            "actor": {"login": "B"},
                        },
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected argv: {argv}")

    created, skipped = scan_assigned(store, runner, now="2026-01-01T00:00:00Z")
    assert skipped == 0
    assert len(created) == 1
    row = store.row("activity", created[0])
    assert row is not None
    assert row["payload"]["assigned_by"] == "A"
    assert row["payload"]["event_id"] == 100

    # `now` here must not advance the watch cursor past the events'
    # `created_at` (both "2026-01-01T00:00:00Z"), or scan_assigned's
    # no-backfill rule filters them out before the tie-break/marker
    # comparison this test exercises ever runs.
    created2, skipped2 = scan_assigned(store, runner, now="2026-08-23T13:00:00Z")
    assert skipped2 == 0
    assert len(created2) == 1
    row2 = store.row("activity", created2[0])
    assert row2 is not None
    assert row2["id"] != created[0]
    assert row2["payload"]["assigned_by"] == "B"
    assert row2["payload"]["event_id"] == 200


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
                            "actor": {"login": "alice"},
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
                            "actor": {"login": "alice"},
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


def test_dispatch_assigned_without_policy_json_is_unchanged(tmp_path: Path) -> None:
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
    assert load_policy(store.home) is None
    assert not (store.home / "policy.json").exists()
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=tmp_path / "sessions",
    )
    assert status == "started"
    assert start_log == [(sid, tmp_path / "sessions" / sid)]
    assert knock_log == ["asg-1"]


def _write_admit_policy(home: Path, **over: object) -> None:
    policy: dict = {
        "actors_allow": ["bob"],
        "repos_allow": ["Owner/repo"],
        "job_types_allow": ["implement"],
    }
    policy.update(over)
    (home / "policy.json").write_text(json.dumps(policy), encoding="utf-8")


def _insert_assigned_activity(
    store: Store,
    *,
    sid: str = "assigned",
    aid: str = "asg-1",
    attached: bool = False,
    assigned_by: str = "bob",
    repo: str = "Owner/repo",
) -> None:
    session: dict = {"id": sid, "kind": "runner", "status": "active"}
    if attached:
        session["runtime"] = {"control": "attached"}
    store.write("session", "insert", sid, session)
    store.write(
        "activity",
        "insert",
        aid,
        {
            "id": aid,
            "session_id": sid,
            "type": "issue.assigned",
            "payload": {
                "repo": repo,
                "number": 8,
                "url": f"https://github.com/{repo}/issues/8",
                "title": "t",
                "body": "SECRET_BODY_DO_NOT_COPY",
                "assigned_by": assigned_by,
                "mandate": "github-assignment",
            },
            "execution_status": "done",
        },
    )


def test_dispatch_assigned_denies_when_policy_rejects_actor(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    _write_admit_policy(tmp_path, actors_allow=["alice"])
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    workspace_root = tmp_path / "sessions"

    def runner(argv: list[str]) -> Completed:
        if ".private" in " ".join(argv):
            return Completed(0, "false", "")
        raise AssertionError(argv)

    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
        runner=runner,
    )
    assert status == "denied"
    assert start_log == []
    assert knock_log == []
    assert not (workspace_root / "assigned" / "MANDATE.md").exists()
    assert store.row("activity", "asg-1") is not None
    assert not store.wake_delivered("asg-1")


def test_dispatch_assigned_denies_when_job_types_allow_omits_implement(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    _write_admit_policy(tmp_path, job_types_allow=["pr-review"])
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []

    def runner(argv: list[str]) -> Completed:
        if ".private" in " ".join(argv):
            return Completed(0, "false", "")
        raise AssertionError(argv)

    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=tmp_path / "sessions",
        runner=runner,
    )
    assert status == "denied"
    assert start_log == []
    assert knock_log == []


def test_dispatch_assigned_denies_when_policy_json_is_null(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    (tmp_path / "policy.json").write_text("null", encoding="utf-8")
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
    assert status == "denied"
    assert start_log == []
    assert knock_log == []
    assert not (workspace_root / "assigned" / "MANDATE.md").exists()
    assert store.row("activity", "asg-1") is not None
    assert not store.wake_delivered("asg-1")


def test_load_policy_raises_on_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "policy.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StoreError, match="invalid JSON"):
        load_policy(tmp_path)


def test_policy_present_false_when_absent(tmp_path: Path) -> None:
    assert policy_present(tmp_path) is False


def test_policy_present_raises_when_policy_json_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / "policy.json").mkdir()
    with pytest.raises(StoreError, match="not a regular file"):
        policy_present(tmp_path)


def test_policy_present_raises_when_policy_json_is_a_broken_symlink(
    tmp_path: Path,
) -> None:
    (tmp_path / "policy.json").symlink_to(tmp_path / "missing-target")
    with pytest.raises(StoreError, match="not a regular file"):
        policy_present(tmp_path)


def test_dispatch_assigned_raises_when_policy_json_is_invalid(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    (tmp_path / "policy.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StoreError, match="invalid JSON"):
        dispatch_assigned(
            store,
            "asg-1",
            sync=lambda: None,
            start=lambda s, cwd: None,
            knock=lambda aid: None,
            workspace_root=tmp_path / "sessions",
        )


def test_dispatch_assigned_denies_when_policy_rejects_attached(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store, attached=True)
    _write_admit_policy(tmp_path, actors_allow=["alice"])
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    workspace_root = tmp_path / "sessions"

    def runner(argv: list[str]) -> Completed:
        if ".private" in " ".join(argv):
            return Completed(0, "false", "")
        raise AssertionError(argv)

    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
        runner=runner,
    )
    assert status == "denied"
    assert start_log == []
    assert knock_log == []
    assert not (workspace_root / "assigned" / "MANDATE.md").exists()
    assert store.row("activity", "asg-1") is not None
    assert not store.wake_delivered("asg-1")


def test_dispatch_assigned_starts_when_policy_admits(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    _write_admit_policy(tmp_path)
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    workspace_root = tmp_path / "sessions"

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if ".private" in joined:
            return Completed(0, "false", "")
        raise AssertionError(argv)

    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
        runner=runner,
    )
    assert status == "started"
    assert start_log == [("assigned", workspace_root / "assigned")]
    assert knock_log == ["asg-1"]


def test_dispatch_assigned_kicks_when_policy_admits_attached(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store, attached=True)
    _write_admit_policy(tmp_path)
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if ".private" in joined:
            return Completed(0, "false", "")
        raise AssertionError(argv)

    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=tmp_path / "sessions",
        runner=runner,
    )
    assert status == "kicked"
    assert start_log == []
    assert knock_log == ["asg-1"]


def test_dispatch_assigned_private_repo_needs_naming_twice(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    _write_admit_policy(tmp_path)
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    workspace_root = tmp_path / "sessions"

    def runner(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if ".private" in joined:
            return Completed(0, "true", "")
        raise AssertionError(argv)

    denied = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
        runner=runner,
    )
    assert denied == "denied"
    assert start_log == []
    assert knock_log == []
    _write_admit_policy(
        tmp_path,
        agent_identity={"private_repos_allow": ["Owner/repo"]},
    )
    admitted = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=workspace_root,
        runner=runner,
    )
    assert admitted == "started"
    assert start_log == [("assigned", workspace_root / "assigned")]
    assert knock_log == ["asg-1"]


def test_dispatch_assigned_denies_when_runner_missing_and_repo_not_private_allowed(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    _insert_assigned_activity(store)
    _write_admit_policy(tmp_path)
    start_log: list[tuple[str, Path]] = []
    knock_log: list[str] = []
    status = dispatch_assigned(
        store,
        "asg-1",
        sync=lambda: None,
        start=lambda s, cwd: start_log.append((s, cwd)),
        knock=lambda aid: knock_log.append(aid),
        workspace_root=tmp_path / "sessions",
    )
    assert status == "denied"
    assert start_log == []
    assert knock_log == []


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
                            "actor": {"login": "alice"},
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
                            "actor": {"login": "alice"},
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


def test_pending_assigned_orders_same_timestamp_rows_by_event_id(
    tmp_path: Path,
) -> None:
    # Two rows can share an assigned_at when a later scan adds a genuinely
    # later same-second event (see _assignment_is_newer) — order those by
    # event_id, not by the random activity uuid. Ids are chosen so that
    # sorting by id alone (the pre-fix behavior) would give the WRONG
    # order, so this genuinely fails without the event_id-aware sort key.
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
        "asg-aaa-higher-event-id",
        {
            "id": "asg-aaa-higher-event-id",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "assigned_at": "2026-06-01T00:00:00Z",
                "event_id": 200,
            },
            "execution_status": "done",
        },
    )
    store.write(
        "activity",
        "insert",
        "asg-zzz-lower-event-id",
        {
            "id": "asg-zzz-lower-event-id",
            "session_id": "assigned",
            "type": "issue.assigned",
            "payload": {
                "repo": "Owner/repo",
                "number": 8,
                "assigned_at": "2026-06-01T00:00:00Z",
                "event_id": 100,
            },
            "execution_status": "done",
        },
    )
    pending = pending_assigned(store, "assigned")
    assert [row["id"] for row in pending] == [
        "asg-zzz-lower-event-id",
        "asg-aaa-higher-event-id",
    ]


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
                            "actor": {"login": "alice"},
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
                            "actor": {"login": "alice"},
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
                            "actor": {"login": "alice"},
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
