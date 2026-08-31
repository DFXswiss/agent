from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.ingest import (
    job_row,
    mentions,
    notification_request,
    requested_job_type,
    scan_mentions,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store

POLICY = {
    "actors_allow": ["davidleomay"],
    "repos_allow": ["owner/name"],
    "job_types_allow": ["pr-review", "pr-ready"],
    "default_skill_on_mention": "pr-review",
}


# ---------------------------------------------------------------- mentions


def test_a_plain_mention_addresses_us() -> None:
    assert mentions("@theo-vane please review", "theo-vane")
    assert mentions("hey @Theo-Vane, look at this", "theo-vane")


def test_a_quoted_mention_is_a_citation_not_an_address() -> None:
    # Someone quoting an earlier request must not trigger a second run of it.
    body = "> @theo-vane please review\n\nI already asked, still waiting."
    assert not mentions(body, "theo-vane")


def test_a_longer_handle_does_not_answer_for_a_shorter_one() -> None:
    assert not mentions("@theo-vane-bot please review", "theo-vane")
    assert mentions("@theo-vane. please review", "theo-vane")


@pytest.mark.parametrize("junk", [None, 42, [], {"body": "x"}])
def test_an_unreadable_body_addresses_nobody(junk: object) -> None:
    assert not mentions(junk, "theo-vane")


def test_an_empty_handle_matches_nothing() -> None:
    # Otherwise a missing login would turn every comment into an address.
    assert not mentions("@theo-vane please review", "")


# ------------------------------------------------------------ job type


def test_an_explicit_job_type_wins_over_the_default() -> None:
    got = requested_job_type("@theo-vane pr-ready please", POLICY, POLICY["job_types_allow"])
    assert got == "pr-ready"


def test_without_an_explicit_type_the_default_applies() -> None:
    got = requested_job_type("@theo-vane please have a look", POLICY, POLICY["job_types_allow"])
    assert got == "pr-review"


def test_a_type_named_only_inside_a_quote_does_not_count() -> None:
    body = "> @theo-vane pr-ready\n\n@theo-vane please have a look"
    assert requested_job_type(body, POLICY, POLICY["job_types_allow"]) == "pr-review"


def test_without_a_default_an_unasked_request_has_no_type() -> None:
    # No default and nothing named: the caller has to skip, not guess.
    assert requested_job_type("@theo-vane hello", {"job_types_allow": ["pr-review"]}, []) is None


# ------------------------------------------------- notification parsing


def test_a_notification_yields_its_repo_and_number() -> None:
    item = {
        "repository": {"full_name": "owner/name"},
        "subject": {"url": "https://api.github.com/repos/owner/name/pulls/5178"},
    }
    assert notification_request(item) == ("owner/name", "5178")


@pytest.mark.parametrize(
    "item",
    [
        None,
        {},
        {"repository": {"full_name": "no-slash"}, "subject": {"url": ".../1"}},
        {"repository": {"full_name": "owner/name"}, "subject": {"url": ".../notanumber"}},
        {"repository": {"full_name": "owner/name"}, "subject": {"url": ".../0"}},
        {"repository": {"full_name": "/name"}, "subject": {"url": ".../1"}},
        {"repository": "owner/name", "subject": {"url": ".../1"}},
    ],
)
def test_an_unusable_notification_yields_nothing(item: object) -> None:
    assert notification_request(item) is None


# ------------------------------------------------------------ scanning


def _runner(
    *,
    notifications: list,
    comments: list,
    private: str = "false",
    notif_rc: int = 0,
    comments_rc: int = 0,
    private_rc: int = 0,
):
    def run(argv: list[str]) -> Completed:
        joined = " ".join(argv)
        if "notifications" in joined:
            return Completed(notif_rc, json.dumps([notifications]), "")
        if "/comments" in joined:
            return Completed(comments_rc, json.dumps([comments]), "")
        if ".private" in joined:
            return Completed(private_rc, private, "")
        raise AssertionError(argv)

    return run


def _notification(repo: str = "owner/name", number: int = 7) -> dict:
    return {
        "repository": {"full_name": repo},
        "subject": {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"},
    }


def _comment(body: str, actor: str = "davidleomay") -> dict:
    return {"body": body, "user": {"login": actor}}


def _jobs(store: Store) -> list[dict]:
    return store.rows("job")


def test_an_admitted_mention_becomes_a_queued_job(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, skipped = scan_mentions(
            store,
            _runner(notifications=[_notification()], comments=[_comment("@theo-vane please review")]),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert len(created) == 1
        assert skipped == 0
        rows = _jobs(store)
        assert len(rows) == 1
        assert rows[0]["state"] == "queued"
        assert rows[0]["repo"] == "owner/name"
        assert rows[0]["ref"] == "7"
        assert rows[0]["job_type"] == "pr-review"
        assert rows[0]["actor"] == "davidleomay"
    finally:
        store.close()


def test_the_same_mention_twice_is_one_job(tmp_path: Path) -> None:
    # What the identity is for: asking again must not queue the work twice.
    store = Store(tmp_path)
    try:
        run = _runner(
            notifications=[_notification()], comments=[_comment("@theo-vane please review")]
        )
        scan_mentions(store, run, session_id="s", login="theo-vane", policy=POLICY)
        created, _ = scan_mentions(store, run, session_id="s", login="theo-vane", policy=POLICY)
        assert created == []
        assert len(_jobs(store)) == 1
    finally:
        store.close()


def test_an_actor_outside_the_policy_queues_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, skipped = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", actor="a-stranger")],
            ),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert created == []
        assert skipped == 1
        assert _jobs(store) == []
    finally:
        store.close()


def test_a_comment_that_does_not_address_us_queues_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, skipped = scan_mentions(
            store,
            _runner(notifications=[_notification()], comments=[_comment("looks good to me")]),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_private_repo_without_the_exception_queues_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, skipped = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private="true",
            ),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_private_repo_named_twice_is_admitted(tmp_path: Path) -> None:
    store = Store(tmp_path)
    policy = dict(POLICY, agent_identity={"private_repos_allow": ["owner/name"]})
    try:
        created, _ = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private="true",
            ),
            session_id="s",
            login="theo-vane",
            policy=policy,
        )
        assert len(created) == 1
    finally:
        store.close()


def test_an_unreadable_visibility_counts_as_private(tmp_path: Path) -> None:
    # Treating an unknown repository as public would send it down the path that
    # skips the private allow-list, which is the one that must not be skipped.
    store = Store(tmp_path)
    try:
        created, skipped = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private_rc=1,
            ),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


@pytest.mark.parametrize("failing", ["notif_rc", "comments_rc"])
def test_a_failing_gh_call_queues_nothing(tmp_path: Path, failing: str) -> None:
    store = Store(tmp_path)
    try:
        created, _ = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                **{failing: 1},
            ),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert created == []
        assert _jobs(store) == []
    finally:
        store.close()


def test_the_newest_addressing_comment_decides(tmp_path: Path) -> None:
    # An older mention asking for something else must not override the latest one.
    store = Store(tmp_path)
    try:
        created, _ = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane pr-ready"),
                    _comment("@theo-vane please review"),
                ],
            ),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert len(created) == 1
        assert _jobs(store)[0]["job_type"] == "pr-review"
    finally:
        store.close()


def test_a_fresh_row_starts_queued_with_no_attempts() -> None:
    row = job_row(session_id="s", repo="owner/name", ref="7", job_type="pr-review", actor="a")
    assert row["state"] == "queued"
    assert row["attempts"] == 0
    assert row["created_at"] == row["updated_at"]


def test_an_empty_handle_matches_nothing_even_where_the_pattern_would() -> None:
    # The earlier case passes for the wrong reason: with an empty login the
    # pattern is `@([^a-z0-9-]|$)`, and `@theo-vane` has a letter after the `@`,
    # so the regex declines on its own. This body is one the pattern WOULD
    # accept, so it exercises the guard rather than the regex.
    assert not mentions("write to @ example.com", "")
    assert not mentions("ping @", "")


def test_a_visibility_answer_that_is_neither_true_nor_false_counts_as_private(
    tmp_path: Path,
) -> None:
    # gh exiting 0 with something unexpected is not a licence to treat the
    # repository as public — that is the path which skips the private list.
    store = Store(tmp_path)
    try:
        created, skipped = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private="null",
            ),
            session_id="s",
            login="theo-vane",
            policy=POLICY,
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_without_a_default_an_unnamed_request_queues_nothing(tmp_path: Path) -> None:
    # No default configured and no known type named: the request is not for
    # anything this instance does, so it must not be guessed into one.
    store = Store(tmp_path)
    policy = {k: v for k, v in POLICY.items() if k != "default_skill_on_mention"}
    try:
        created, skipped = scan_mentions(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane can you look at this?")],
            ),
            session_id="s",
            login="theo-vane",
            policy=policy,
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_admits_rejects_a_missing_job_type_on_its_own() -> None:
    # This is why the guard above it cannot be pinned by a mutation: the
    # admission gate already refuses, so removing the guard changes nothing.
    from agent_cli.jobs import admits

    v = admits(POLICY, actor="davidleomay", repo="owner/name", job_type="", private=False)
    assert not v.admitted
    assert "job type" in v.reason


def test_a_mention_shown_as_code_is_an_example_not_an_address() -> None:
    # Documenting how to call this bot must not call it. A fenced block and an
    # inline span are both ways of displaying a handle rather than using it.
    fenced = "call it like this:\n```\n@theo-vane please review\n```\nthat is all"
    assert not mentions(fenced, "theo-vane")
    assert not mentions("write ~~~\n@theo-vane pr-ready\n~~~ to ask", "theo-vane")
    assert not mentions("the handle is `@theo-vane`, use it in a comment", "theo-vane")
    # A real request alongside an example still counts.
    assert mentions("`@theo-vane` is the handle — @theo-vane please review", "theo-vane")


def test_a_job_type_named_only_inside_code_does_not_count() -> None:
    body = "the type is `pr-ready` — @theo-vane please review"
    assert requested_job_type(body, POLICY, POLICY["job_types_allow"]) == "pr-review"


def test_a_longer_job_type_is_not_matched_by_its_prefix() -> None:
    # `pr-review` must not match inside `pr-review-deep`, whatever order the
    # allow-list happens to be in.
    allowed = ["pr-review", "pr-review-deep"]
    assert requested_job_type("@x pr-review-deep please", POLICY, allowed) == "pr-review-deep"
    assert (
        requested_job_type("@x pr-review-deep please", POLICY, list(reversed(allowed)))
        == "pr-review-deep"
    )
