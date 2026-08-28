from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli.github_act import ACTIVITY_MARKER, scan_github
from agent_cli.main import main
from agent_cli.runtime import Completed
from agent_cli.store import Store


def run(home: Path, argv: list[str]) -> None:
    import os

    os.environ["AGENT_HOME"] = str(home)
    main(argv)


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


def test_pr_open_create_then_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-1"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "Add github pending",
            "head": "feat-github",
            "body": "Please review",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(1, "", "no pull requests found")
        if "create" in argv:
            assert "--draft" in argv
            assert "--repo" in argv
            assert "dfxswiss/agent" in argv
            body = argv[argv.index("--body") + 1]
            assert ACTIVITY_MARKER.format(id=act_id) in body
            return Completed(0, "https://github.com/dfxswiss/agent/pull/42\n", "")
        raise AssertionError(f"unexpected argv: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} done number=42"]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["number"] == 42
    assert row["result"]["draft"] is True
    assert row["result"]["url"] == "https://github.com/dfxswiss/agent/pull/42"
    assert any("create" in c for c in calls)

    calls.clear()

    def runner2(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            body = {
                "number": 42,
                "url": "https://github.com/dfxswiss/agent/pull/42",
                "state": "OPEN",
                "isDraft": True,
            }
            return Completed(0, json.dumps(body), "")
        raise AssertionError(f"unexpected argv on retry: {argv}")

    # Already done — pending_work skips it; force another scan on a fresh pending clone.
    assert scan_github(store, runner2) == []
    assert not any("create" in c for c in calls)

    act2 = "pr-2"
    _pending(
        store,
        act2,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "Again",
            "head": "feat-github",
        },
    )
    calls.clear()

    def runner3(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            body = {
                "number": 42,
                "url": "https://github.com/dfxswiss/agent/pull/42",
                "state": "OPEN",
                "isDraft": True,
            }
            return Completed(0, json.dumps(body), "")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner3)
    assert lines == [f"pr.open {act2} done number=42"]
    assert not any("create" in c for c in calls)
    row2 = store.row("activity", act2)
    assert row2 is not None
    assert row2["result"]["draft"] is True


def test_pr_open_view_auth_error_no_create(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-auth"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(1, "", "HTTP 401")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert not any("create" in c for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert "HTTP 401" in (row.get("execution_error") or "")


def test_pr_open_view_token_not_found_no_create(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-token"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(1, "", "authentication token not found")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert not any("create" in c for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"


def test_pr_open_view_generic_http_404_no_create(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-404"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(1, "", "Not Found (HTTP 404)")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert not any("create" in c for c in calls)


def test_pr_open_existing_merged_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-merged"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            body = {
                "number": 9,
                "url": "https://github.com/dfxswiss/agent/pull/9",
                "state": "MERGED",
                "isDraft": False,
            }
            return Completed(0, json.dumps(body), "")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert not any("create" in c for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert "not open" in (row.get("execution_error") or "")


def test_pr_open_existing_not_draft_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-ready"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            body = {
                "number": 42,
                "url": "https://github.com/dfxswiss/agent/pull/42",
                "state": "OPEN",
                "isDraft": False,
            }
            return Completed(0, json.dumps(body), "")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert not any("create" in c for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert "existing pull request is not a draft" in (row.get("execution_error") or "")
    assert row.get("result") is None or "number" not in (row.get("result") or {})


def test_pr_open_base_wrong_type_no_gh(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-base"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
            "base": 1,
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "{}", "")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert calls == []
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"


def test_pr_open_base_empty_string_no_gh(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-base-empty"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
            "base": "",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "{}", "")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert calls == []
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert "base must be a non-empty string" in (row.get("execution_error") or "")


def test_pr_open_missing_title_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-bad"
    _pending(
        store,
        act_id,
        "pr.open",
        {"repo": "dfxswiss/agent", "head": "feat"},
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "{}", "")

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    assert calls == []
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert "result" not in row or row.get("result") is None


def test_pr_open_create_nonzero_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "pr-fail"
    _pending(
        store,
        act_id,
        "pr.open",
        {
            "repo": "dfxswiss/agent",
            "title": "T",
            "head": "feat",
        },
    )

    def runner(argv: list[str]) -> Completed:
        if argv[:3] == ["gh", "pr", "view"]:
            return Completed(1, "", "no pull requests found")
        if "create" in argv:
            return Completed(1, "", "create failed")
        raise AssertionError(argv)

    lines = scan_github(store, runner)
    assert lines == [f"pr.open {act_id} error"]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert row.get("result") is None or "number" not in (row.get("result") or {})


def test_issue_write_create_and_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "issue-1"
    marker = ACTIVITY_MARKER.format(id=act_id)
    _pending(
        store,
        act_id,
        "issue.write",
        {"repo": "dfxswiss/agent", "title": "Track work", "body": "details"},
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps([]), "")
        if argv[:3] == ["gh", "issue", "create"]:
            body = argv[argv.index("--body") + 1]
            assert marker in body
            return Completed(0, "https://github.com/dfxswiss/agent/issues/7\n", "")
        raise AssertionError(argv)

    lines = scan_github(store, runner)
    assert lines == [f"issue.write {act_id} done number=7"]
    assert any(c[:3] == ["gh", "issue", "create"] for c in calls)

    act2 = "issue-2"
    marker2 = ACTIVITY_MARKER.format(id=act2)
    _pending(
        store,
        act2,
        "issue.write",
        {"repo": "dfxswiss/agent", "title": "Track work again"},
    )
    calls.clear()

    def runner2(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "number": 7,
                            "title": "Track work",
                            "url": "https://github.com/dfxswiss/agent/issues/7",
                            "body": f"details\n{marker2}",
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner2)
    assert lines == [f"issue.write {act2} done number=7"]
    assert not any("create" in c for c in calls)


def test_issue_write_list_truncated_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "issue-trunc"
    _pending(
        store,
        act_id,
        "issue.write",
        {"repo": "dfxswiss/agent", "title": "Track work"},
    )
    calls: list[list[str]] = []
    listed = [
        {
            "number": i,
            "title": f"Issue {i}",
            "url": f"https://github.com/dfxswiss/agent/issues/{i}",
            "body": "no marker here",
        }
        for i in range(1, 101)
    ]

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[:3] == ["gh", "issue", "list"]:
            return Completed(0, json.dumps(listed), "")
        raise AssertionError(f"create must not run: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"issue.write {act_id} error"]
    assert not any("create" in c for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"
    assert "issue list truncated" in (row.get("execution_error") or "")


def test_comment_post_first_then_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "c-1"
    marker = ACTIVITY_MARKER.format(id=act_id)
    comment_url = "https://github.com/dfxswiss/agent/issues/3#issuecomment-9"
    _pending(
        store,
        act_id,
        "comment.post",
        {
            "repo": "dfxswiss/agent",
            "number": 3,
            "body": "looks good",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[1] == "api":
            assert "--paginate" in argv
            assert "--slurp" in argv
            assert argv[-1] == "repos/dfxswiss/agent/issues/3/comments"
            return Completed(0, json.dumps([]), "")
        if argv[:3] == ["gh", "issue", "comment"]:
            body = argv[argv.index("--body") + 1]
            assert marker in body
            return Completed(0, f"{comment_url}\n", "")
        raise AssertionError(argv)

    lines = scan_github(store, runner)
    assert lines == [f"comment.post {act_id} done"]
    assert any(c[:3] == ["gh", "issue", "comment"] for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["number"] == 3
    assert row["result"]["url"] == comment_url

    act2 = "c-2"
    marker2 = ACTIVITY_MARKER.format(id=act2)
    _pending(
        store,
        act2,
        "comment.post",
        {
            "repo": "dfxswiss/agent",
            "number": 3,
            "body": "again",
            "target": "pr",
        },
    )
    calls.clear()

    def runner2(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[1] == "api":
            assert "--paginate" in argv
            assert "--slurp" in argv
            assert "repos/dfxswiss/agent/issues/3/comments" in argv
            return Completed(
                0,
                json.dumps(
                    [
                        {
                            "id": 99,
                            "body": f"x\n{marker2}",
                            "html_url": "https://github.com/dfxswiss/agent/issues/3#issuecomment-99",
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"comment must not post again: {argv}")

    lines = scan_github(store, runner2)
    assert lines == [f"comment.post {act2} done"]
    assert not any(c[:3] == ["gh", "pr", "comment"] for c in calls)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in calls)
    row2 = store.row("activity", act2)
    assert row2 is not None
    assert row2["result"]["id"] == 99


def test_comment_post_paginate_finds_marker(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "c-page"
    marker = ACTIVITY_MARKER.format(id=act_id)
    _pending(
        store,
        act_id,
        "comment.post",
        {
            "repo": "dfxswiss/agent",
            "number": 3,
            "body": "looks good",
        },
    )
    calls: list[list[str]] = []
    page1 = [
        {"id": i, "body": f"old {i}", "html_url": f"https://github.com/dfxswiss/agent/issues/3#issuecomment-{i}"}
        for i in range(1, 31)
    ]
    page2 = [
        {
            "id": 31,
            "body": f"found\n{marker}",
            "html_url": "https://github.com/dfxswiss/agent/issues/3#issuecomment-31",
        }
    ]

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[1] == "api":
            assert "--paginate" in argv
            assert "--slurp" in argv
            assert argv[-1] == "repos/dfxswiss/agent/issues/3/comments"
            return Completed(0, json.dumps([page1, page2]), "")
        raise AssertionError(f"comment must not post: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"comment.post {act_id} done"]
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in calls)
    assert not any(c[:3] == ["gh", "pr", "comment"] for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["id"] == 31
    assert row["result"]["url"] == "https://github.com/dfxswiss/agent/issues/3#issuecomment-31"


def test_comment_post_slurp_non_dict_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "c-shape"
    _pending(
        store,
        act_id,
        "comment.post",
        {
            "repo": "dfxswiss/agent",
            "number": 3,
            "body": "x",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[1] == "api":
            return Completed(0, json.dumps([[{"id": 1, "body": "ok"}, None]]), "")
        raise AssertionError(f"comment must not post: {argv}")

    lines = scan_github(store, runner)
    assert lines == [f"comment.post {act_id} error"]
    assert not any("comment" in c and c[:3] != ["gh", "api"] for c in calls if c)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"


def test_comment_post_target_pr_posts(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "c-pr"
    marker = ACTIVITY_MARKER.format(id=act_id)
    comment_url = "https://github.com/dfxswiss/agent/pull/8#issuecomment-12"
    _pending(
        store,
        act_id,
        "comment.post",
        {
            "repo": "dfxswiss/agent",
            "number": 8,
            "body": "on the pr",
            "target": "pr",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if argv[1] == "api":
            assert argv[-1] == "repos/dfxswiss/agent/issues/8/comments"
            return Completed(0, json.dumps([]), "")
        if argv[:3] == ["gh", "pr", "comment"]:
            body = argv[argv.index("--body") + 1]
            assert marker in body
            assert "8" in argv
            return Completed(0, f"{comment_url}\n", "")
        raise AssertionError(argv)

    lines = scan_github(store, runner)
    assert lines == [f"comment.post {act_id} done"]
    assert any(c[:3] == ["gh", "pr", "comment"] for c in calls)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in calls)
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["url"] == comment_url


def test_comment_post_invalid_target_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "c-bad"
    _pending(
        store,
        act_id,
        "comment.post",
        {
            "repo": "dfxswiss/agent",
            "number": 3,
            "body": "x",
            "target": "commit",
        },
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "{}", "")

    lines = scan_github(store, runner)
    assert lines == [f"comment.post {act_id} error"]
    assert calls == []
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"


def test_comment_post_non_positive_number_errors(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    for act_id, number in (("c-zero", 0), ("c-neg", -1)):
        _pending(
            store,
            act_id,
            "comment.post",
            {
                "repo": "dfxswiss/agent",
                "number": number,
                "body": "x",
            },
        )
        calls: list[list[str]] = []

        def runner(argv: list[str]) -> Completed:
            calls.append(list(argv))
            return Completed(0, "{}", "")

        lines = scan_github(store, runner)
        assert lines == [f"comment.post {act_id} error"]
        assert calls == []
        row = store.row("activity", act_id)
        assert row is not None
        assert row["execution_status"] == "error"


def test_subscription_set_ignored(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    _pending(
        store,
        "sub-1",
        "subscription.set",
        {"subscriptions": [{"match": {"type": "message"}}]},
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, "{}", "")

    assert scan_github(store, runner) == []
    assert calls == []
    row = store.row("activity", "sub-1")
    assert row is not None
    assert row["execution_status"] == "pending"


def test_cli_github_usage() -> None:
    with pytest.raises(SystemExit, match="github pending"):
        main(["github"])
    with pytest.raises(SystemExit, match="github pending"):
        main(["github", "nope"])


def test_cli_github_pending_none(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(tmp_path, ["init"])
    run(tmp_path, ["github", "pending"])
    assert "github pending none" in capsys.readouterr().out


def test_review_post_submits_a_comment_review_then_is_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "r-1"
    marker = ACTIVITY_MARKER.format(id=act_id)
    review_url = "https://github.com/dfxswiss/agent/pull/3#pullrequestreview-77"
    _pending(
        store,
        act_id,
        "review.post",
        {"repo": "dfxswiss/agent", "number": 3, "body": "dto.ts:91 wrong decorator"},
    )
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        calls.append(list(argv))
        if "-X" in argv and argv[argv.index("-X") + 1] == "POST":
            assert argv[-4] == "repos/dfxswiss/agent/pulls/3/reviews" or (
                "repos/dfxswiss/agent/pulls/3/reviews" in argv
            )
            joined = " ".join(argv)
            # A bot that can hold a merge closed is a different tool from one that reports.
            assert "event=COMMENT" in joined
            assert "event=REQUEST_CHANGES" not in joined
            assert marker in joined
            return Completed(0, json.dumps({"id": 77, "html_url": review_url}), "")
        if argv[-1] == "user":
            return Completed(0, json.dumps({"login": "theo-vane"}), "")
        if argv[1] == "api":
            assert argv[-1] == "repos/dfxswiss/agent/pulls/3/reviews"
            return Completed(0, json.dumps([]), "")
        raise AssertionError(argv)

    assert scan_github(store, runner) == [f"review.post {act_id} done"]
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "done"
    assert row["result"]["url"] == review_url

    # A second activity whose marker is already on the pull request must not post again.
    act2 = "r-2"
    _pending(
        store,
        act2,
        "review.post",
        {"repo": "dfxswiss/agent", "number": 3, "body": "same finding"},
    )
    existing = [
        {
            "id": 77,
            "html_url": review_url,
            "body": ACTIVITY_MARKER.format(id=act2),
            # Anders geschrieben als das Konto aus `gh api user`: GitHub-Logins sind
            # nicht gross-/kleinschreibungsempfindlich, der Vergleich darf es auch nicht sein.
            "user": {"login": "Theo-Vane"},
        }
    ]
    posts: list[list[str]] = []

    def runner2(argv: list[str]) -> Completed:
        if "-X" in argv and argv[argv.index("-X") + 1] == "POST":
            posts.append(list(argv))
            raise AssertionError("must not post twice")
        if argv[-1] == "user":
            return Completed(0, json.dumps({"login": "theo-vane"}), "")
        return Completed(0, json.dumps([existing]), "")

    assert scan_github(store, runner2) == [f"review.post {act2} done"]
    assert posts == []


def test_review_post_is_an_executable_activity_type() -> None:
    # Without this the row is written, nothing ever runs it, and the findings never
    # reach the pull request — silently, which is the defect this path exists to fix.
    from agent_cli.store import EXECUTABLE_ACTIVITY_TYPES

    assert "review.post" in EXECUTABLE_ACTIVITY_TYPES


def test_review_post_ignores_a_marker_in_someone_elses_review(tmp_path: Path) -> None:
    # The marker is visible to anyone reading the pull request. Treating a copy of it
    # as our own delivery would suppress the findings entirely.
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "r-3"
    _pending(
        store,
        act_id,
        "review.post",
        {"repo": "dfxswiss/agent", "number": 3, "body": "a finding"},
    )
    foreign = [
        {
            "id": 5,
            "html_url": "https://example.invalid/x",
            "body": "copied " + ACTIVITY_MARKER.format(id=act_id),
            "user": {"login": "somebody-else"},
        }
    ]
    posted: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        if "-X" in argv and argv[argv.index("-X") + 1] == "POST":
            posted.append(list(argv))
            return Completed(0, json.dumps({"id": 9, "html_url": "https://example.invalid/ours"}), "")
        if argv[-1] == "user":
            return Completed(0, json.dumps({"login": "theo-vane"}), "")
        return Completed(0, json.dumps([foreign]), "")

    assert scan_github(store, runner) == [f"review.post {act_id} done"]
    assert len(posted) == 1, "a foreign marker must not stand in for our own review"


def test_review_post_drops_an_unusable_review_id(tmp_path: Path) -> None:
    # jq/gh can hand back anything; an id is a positive integer or it is not recorded.
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "r-4"
    _pending(
        store,
        act_id,
        "review.post",
        {"repo": "dfxswiss/agent", "number": 3, "body": "a finding"},
    )

    def runner(argv: list[str]) -> Completed:
        if "-X" in argv and argv[argv.index("-X") + 1] == "POST":
            return Completed(
                0,
                json.dumps({"id": {"nope": 1}, "html_url": "https://example.invalid/ours"}),
                "",
            )
        if argv[-1] == "user":
            return Completed(0, json.dumps({"login": "theo-vane"}), "")
        return Completed(0, json.dumps([]), "")

    assert scan_github(store, runner) == [f"review.post {act_id} done"]
    row = store.row("activity", act_id)
    assert row is not None
    assert "id" not in row["result"]
    assert row["result"]["url"] == "https://example.invalid/ours"


def test_review_post_does_not_ask_who_we_are_without_a_candidate(tmp_path: Path) -> None:
    # On the ordinary path no review carries our marker, so identity is never needed.
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "r-5"
    _pending(
        store, act_id, "review.post",
        {"repo": "dfxswiss/agent", "number": 3, "body": "a finding"},
    )
    seen: list[str] = []

    def runner(argv: list[str]) -> Completed:
        seen.append(argv[-1])
        if "-X" in argv and argv[argv.index("-X") + 1] == "POST":
            return Completed(0, json.dumps({"id": 9, "html_url": "https://example.invalid/x"}), "")
        return Completed(0, json.dumps([[]]), "")

    assert scan_github(store, runner) == [f"review.post {act_id} done"]
    assert "user" not in seen, "identity was fetched although no marker matched"


def test_review_post_retries_when_identity_cannot_be_established(tmp_path: Path) -> None:
    # A flaked identity lookup must not turn into a second review on the pull request.
    store = Store(tmp_path)
    _owned_session(store)
    act_id = "r-6"
    _pending(
        store, act_id, "review.post",
        {"repo": "dfxswiss/agent", "number": 3, "body": "a finding"},
    )
    ours = [{"id": 4, "html_url": "https://example.invalid/ours",
             "body": ACTIVITY_MARKER.format(id=act_id), "user": {"login": "theo-vane"}}]
    posted: list[list[str]] = []

    def runner(argv: list[str]) -> Completed:
        if "-X" in argv and argv[argv.index("-X") + 1] == "POST":
            posted.append(list(argv))
            return Completed(0, json.dumps({"id": 9, "html_url": "https://example.invalid/dup"}), "")
        if argv[-1] == "user":
            return Completed(1, "", "gh: transient failure")
        return Completed(0, json.dumps([ours]), "")

    assert scan_github(store, runner) == [f"review.post {act_id} error"]
    assert posted == [], "a flaked identity lookup posted a duplicate review"
    row = store.row("activity", act_id)
    assert row is not None
    assert row["execution_status"] == "error"


def test_the_scanner_documents_every_type_it_executes() -> None:
    # A docstring that lists the handled types is a claim about behaviour. This change
    # has already corrected it in three places; a fourth was missed until a lens read it.
    import inspect

    from agent_cli import github_act

    doc = inspect.getdoc(github_act.scan_github) or ""
    module_doc = inspect.getdoc(github_act) or ""
    for typ in ("pr.open", "comment.post", "review.post", "issue.write"):
        assert typ in doc, f"{typ} fehlt im scan_github-Docstring"
        assert typ in module_doc, f"{typ} fehlt im Modul-Docstring"
