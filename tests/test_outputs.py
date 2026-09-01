from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_cli.outputs import (
    baseline_argv,
    kinds_argv,
    parse_baseline_ids,
    parse_new_kinds,
)


# ---------------------------------------------------------------- argv / _target


def test_baseline_argv_returns_exact_11_element_list() -> None:
    # The caller must receive the literal argv shape the module promises.
    got = baseline_argv("owner/name", "123")
    assert got == [
        "gh",
        "api",
        "graphql",
        "-f",
        "query=query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issueOrPullRequest(number:$number){__typename ... on Issue{comments(last:100){nodes{id}}}... on PullRequest{comments(last:100){nodes{id}}reviews(last:100){nodes{id state}}}}}}",
        "-F",
        "owner=owner",
        "-F",
        "name=name",
        "-F",
        "number=123",
    ]


def test_kinds_argv_returns_exact_11_element_list() -> None:
    # The caller must receive the literal argv shape the module promises.
    got = kinds_argv("owner/name", "123")
    assert got == [
        "gh",
        "api",
        "graphql",
        "-f",
        "query=query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issueOrPullRequest(number:$number){__typename ... on Issue{comments(last:100){nodes{id author{login} createdAt}}}... on PullRequest{comments(last:100){nodes{id author{login} createdAt}}reviews(last:100){nodes{id author{login} createdAt submittedAt state}}}}}}",
        "-F",
        "owner=owner",
        "-F",
        "name=name",
        "-F",
        "number=123",
    ]


def test_ref_with_and_without_hash_give_identical_argv() -> None:
    # Both `#123` and `123` are accepted and must produce the same argv.
    assert baseline_argv("owner/name", "#123") == baseline_argv("owner/name", "123")
    assert kinds_argv("owner/name", "#123") == kinds_argv("owner/name", "123")


def test_target_rejects_no_slash() -> None:
    with pytest.raises(ValueError, match="_target requires owner/name and a numeric ref"):
        baseline_argv("ownername", "123")


def test_target_rejects_empty_owner() -> None:
    with pytest.raises(ValueError, match="_target requires owner/name and a numeric ref"):
        baseline_argv("/name", "123")


def test_target_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="_target requires owner/name and a numeric ref"):
        baseline_argv("owner/", "123")


def test_target_rejects_more_than_one_slash() -> None:
    with pytest.raises(ValueError, match="_target requires owner/name and a numeric ref"):
        baseline_argv("a/b/c", "123")


def test_target_rejects_non_numeric_ref() -> None:
    with pytest.raises(ValueError, match="_target requires owner/name and a numeric ref"):
        baseline_argv("owner/name", "abc")


# ---------------------------------------------------------------- payload helper


def _payload(
    *,
    comments: list[dict] | None = None,
    reviews: list[dict] | None = None,
    typename: str = "PullRequest",
) -> dict:
    item: dict = {"__typename": typename, "comments": {"nodes": comments or []}}
    if reviews is not None or typename == "PullRequest":
        item["reviews"] = {"nodes": reviews or []}
    return {"data": {"repository": {"issueOrPullRequest": item}}}


# ---------------------------------------------------------------- parse_baseline_ids


def test_parse_baseline_ids_returns_comments_and_non_pending_reviews() -> None:
    # Both comment ids and submitted review ids must appear in the result.
    payload = _payload(
        comments=[{"id": "c1"}],
        reviews=[{"id": "r1", "state": "APPROVED"}],
    )
    assert parse_baseline_ids(payload) == ["c1", "r1"]


def test_parse_baseline_ids_excludes_pending_review() -> None:
    # A PENDING review must be omitted so its later submission is not masked.
    payload = _payload(reviews=[{"id": "r1", "state": "PENDING"}])
    assert parse_baseline_ids(payload) == []


def test_parse_baseline_ids_issue_returns_only_comments() -> None:
    # An Issue has no reviews key; the parser must still succeed.
    payload = _payload(comments=[{"id": "c1"}], typename="Issue")
    assert parse_baseline_ids(payload) == ["c1"]


def test_parse_baseline_ids_returns_none_for_missing_issue_or_pr() -> None:
    assert parse_baseline_ids({}) is None


def test_parse_baseline_ids_returns_none_for_non_list_comments_nodes() -> None:
    payload = {"data": {"repository": {"issueOrPullRequest": {"__typename": "Issue", "comments": {"nodes": None}}}}}
    assert parse_baseline_ids(payload) is None


def test_parse_baseline_ids_returns_none_for_unknown_typename() -> None:
    payload = _payload(typename="Discussion")
    assert parse_baseline_ids(payload) is None


def test_parse_baseline_ids_returns_none_when_pr_reviews_nodes_is_not_list() -> None:
    payload = {"data": {"repository": {"issueOrPullRequest": {"__typename": "PullRequest", "comments": {"nodes": []}, "reviews": {"nodes": None}}}}}
    assert parse_baseline_ids(payload) is None


def test_parse_baseline_ids_returns_none_for_empty_id() -> None:
    payload = _payload(comments=[{"id": ""}])
    assert parse_baseline_ids(payload) is None


def test_parse_baseline_ids_returns_none_for_non_string_id() -> None:
    payload = _payload(comments=[{"id": 123}])
    assert parse_baseline_ids(payload) is None


# ---------------------------------------------------------------- parse_new_kinds


def test_parse_new_kinds_detects_new_comment() -> None:
    payload = _payload(comments=[{"id": "c1", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z"}])
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T09:00:00Z") == {"comment"}


def test_parse_new_kinds_detects_new_submitted_review() -> None:
    payload = _payload(reviews=[{"id": "r1", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z", "state": "APPROVED"}])
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T09:00:00Z") == {"review"}


def test_parse_new_kinds_detects_both_comment_and_review() -> None:
    payload = _payload(
        comments=[{"id": "c1", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z"}],
        reviews=[{"id": "r1", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z", "state": "APPROVED"}],
    )
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T09:00:00Z") == {"comment", "review"}


def test_parse_new_kinds_ignores_id_already_in_baseline() -> None:
    payload = _payload(comments=[{"id": "c1", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z"}])
    assert parse_new_kinds(payload, login="me", baseline=["c1"], since="2026-09-01T09:00:00Z") == set()


def test_parse_new_kinds_ignores_foreign_author() -> None:
    payload = _payload(comments=[{"id": "c1", "author": {"login": "other"}, "createdAt": "2026-09-01T10:00:00Z"}])
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T09:00:00Z") == set()


def test_parse_new_kinds_ignores_older_than_since() -> None:
    payload = _payload(comments=[{"id": "c1", "author": {"login": "me"}, "createdAt": "2026-09-01T09:00:00Z"}])
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T10:00:00Z") == set()


def test_parse_new_kinds_includes_exactly_at_since() -> None:
    payload = _payload(comments=[{"id": "c1", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z"}])
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T10:00:00Z") == {"comment"}


def test_parse_new_kinds_submitted_at_beats_created_at() -> None:
    # A review drafted before since but submitted after since must count.
    payload = _payload(
        reviews=[
            {
                "id": "r1",
                "author": {"login": "me"},
                "createdAt": "2026-09-01T09:00:00Z",
                "submittedAt": "2026-09-01T11:00:00Z",
                "state": "APPROVED",
            }
        ]
    )
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T10:00:00Z") == {"review"}


def test_parse_new_kinds_pending_review_never_counts() -> None:
    payload = _payload(
        reviews=[
            {
                "id": "r1",
                "author": {"login": "me"},
                "createdAt": "2026-09-01T10:00:00Z",
                "state": "PENDING",
            }
        ]
    )
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T09:00:00Z") == set()


def test_parse_new_kinds_well_formed_nothing_qualifies_returns_empty_set() -> None:
    payload = _payload()
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T10:00:00Z") == set()


def test_parse_new_kinds_returns_none_for_malformed_payload() -> None:
    assert parse_new_kinds({}, login="me", baseline=[], since="2026-09-01T10:00:00Z") is None


def test_parse_new_kinds_returns_none_for_unparseable_since() -> None:
    payload = _payload()
    assert parse_new_kinds(payload, login="me", baseline=[], since="not-a-time") is None


def test_parse_new_kinds_returns_none_for_missing_timestamp_on_own_node() -> None:
    payload = _payload(comments=[{"id": "c1", "author": {"login": "me"}}])
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T10:00:00Z") is None


def test_parse_new_kinds_foreign_missing_timestamp_does_not_force_none() -> None:
    # A third-party node with a bad timestamp must not invalidate the whole answer.
    payload = _payload(
        comments=[
            {"id": "c1", "author": {"login": "other"}},
            {"id": "c2", "author": {"login": "me"}, "createdAt": "2026-09-01T10:00:00Z"},
        ]
    )
    assert parse_new_kinds(payload, login="me", baseline=[], since="2026-09-01T09:00:00Z") == {"comment"}
