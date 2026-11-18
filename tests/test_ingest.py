from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli.ingest import (
    ListingFailed,
    Skill,
    _comment_author,
    _patterns,
    _skill_of,
    allowed_skills,
    catalog_skills,
    default_skill,
    job_row,
    mark_read_argv,
    mentions,
    notification_request,
    notification_thread_id,
    resolve_skill,
    scan_mentions,
    trigger_comment_id,
)
from agent_cli.runtime import Completed
from agent_cli.store import Store, StoreConnectionError, StoreError

HEAD = "a" * 40
OTHER_HEAD = "b" * 40

CATALOG: dict[str, Any] = {
    "skills": [
        {
            "id": "pr-review",
            "queue_type": "pr-ready",
            "command_prefixes": ["/review"],
            "body_patterns": ["please review"],
        },
        {
            "id": "implement-fix",
            "queue_type": "work-fix",
            "command_prefixes": ["/fix"],
            "body_patterns": ["please fix"],
        },
    ]
}

POLICY: dict[str, Any] = {
    "actors_allow": ["davidleomay"],
    "repos_allow": ["owner/name"],
    "job_types_allow": ["pr-ready"],
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


# ------------------------------------------------------------ catalogue


def test_catalog_skills_reads_a_list() -> None:
    entries = catalog_skills({"skills": [{"id": "a"}, None, {"id": "b"}]})
    assert entries == [{"id": "a"}, {"id": "b"}]


def test_catalog_skills_reads_a_mapping_by_its_values() -> None:
    entries = catalog_skills({"skills": {"first": {"id": "a"}, "second": {"id": "b"}}})
    assert entries == [{"id": "a"}, {"id": "b"}]


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        [],
        "skills",
        {},
        {"skills": "not a list or mapping"},
        {"skills": [{"id": "a"}, "not a dict"]},
    ],
)
def test_catalog_skills_is_none_when_unreadable(catalog: object) -> None:
    assert catalog_skills(catalog) is None


def test_skill_of_ignores_an_entry_that_is_not_an_object() -> None:
    # The catalogue reader refuses a non-object entry, but `_skill_of` is also
    # called on entries directly: a value that is not an object names no skill.
    assert _skill_of(None) is None
    assert _skill_of("not an entry") is None
    assert _skill_of({"id": "pr-review", "queue_type": "pr-ready"}) == Skill(
        "pr-review", "pr-ready"
    )


def test_default_skill_is_found_case_insensitively_and_trimmed() -> None:
    policy = {"default_skill_on_mention": " PR-Review "}
    assert default_skill(policy, CATALOG) == Skill("pr-review", "pr-ready")


@pytest.mark.parametrize(
    "policy",
    [
        {},
        {"default_skill_on_mention": None},
        {"default_skill_on_mention": ""},
        {"default_skill_on_mention": "not-in-catalogue"},
    ],
)
def test_default_skill_is_none_when_the_policy_names_no_catalogue_skill(
    policy: dict[str, Any],
) -> None:
    assert default_skill(policy, CATALOG) is None


@pytest.mark.parametrize("policy", [None, [], "policy"])
def test_default_skill_is_none_when_the_policy_is_not_an_object(
    policy: object,
) -> None:
    assert default_skill(policy, CATALOG) is None


def test_allowed_skills_maps_allowed_queue_types_to_skill_ids() -> None:
    skills, unknown = allowed_skills(POLICY, CATALOG)
    assert skills == [Skill("pr-review", "pr-ready")]
    assert unknown == []


def test_allowed_skills_is_case_insensitive() -> None:
    skills, unknown = allowed_skills({"job_types_allow": ["PR-READY"]}, CATALOG)
    assert skills == [Skill("pr-review", "pr-ready")]
    assert unknown == []


def test_allowed_skills_reports_a_queue_type_no_skill_carries() -> None:
    skills, unknown = allowed_skills(
        {"job_types_allow": ["pr-ready", "nothing"]}, CATALOG
    )
    assert skills == [Skill("pr-review", "pr-ready")]
    assert unknown == ["nothing"]


def test_allowed_skills_ignores_a_skill_without_a_queue_type() -> None:
    catalog = {"skills": [{"id": "pr-review"}, {"id": "implement-fix"}]}
    skills, unknown = allowed_skills({"job_types_allow": ["pr-ready"]}, catalog)
    assert skills == []
    assert unknown == ["pr-ready"]


@pytest.mark.parametrize(
    "policy",
    [None, {}, {"job_types_allow": None}, {"job_types_allow": "pr-ready"}],
)
def test_allowed_skills_is_empty_when_the_policy_allows_no_list(
    policy: object,
) -> None:
    # Admission treats a missing allow-list as "admits nothing", so there is no
    # matched skill and no unknown type to report either.
    assert allowed_skills(policy, CATALOG) == ([], [])


def test_allowed_skills_ignores_unusable_allow_list_entries() -> None:
    skills, unknown = allowed_skills(
        {"job_types_allow": [7, "", "   "]}, CATALOG
    )
    assert skills == []
    assert unknown == []


def test_patterns_drops_a_non_string_item_and_a_blank_item() -> None:
    assert _patterns(
        {"command_prefixes": [7, "  ", "/review"]}, "command_prefixes"
    ) == ["/review"]
    # A value that is not a list names no pattern at all.
    assert _patterns({"command_prefixes": "not a list"}, "command_prefixes") == []


# ------------------------------------------------------------ resolve_skill


def test_a_slash_command_prefix_resolves_a_skill() -> None:
    assert resolve_skill("@theo-vane /review please", CATALOG) == Skill(
        "pr-review", "pr-ready"
    )


def test_a_body_phrase_resolves_a_skill() -> None:
    assert resolve_skill("@theo-vane please review this", CATALOG) == Skill(
        "pr-review", "pr-ready"
    )


def test_the_longest_pattern_wins() -> None:
    catalog = {
        "skills": [
            {"id": "short", "body_patterns": ["review"]},
            {"id": "long", "body_patterns": ["please review"]},
        ]
    }
    assert resolve_skill("@theo-vane please review", catalog) == Skill("long", None)


def test_a_tie_goes_to_the_lower_skill_id() -> None:
    catalog = {
        "skills": [
            {"id": "zeta", "body_patterns": ["review"]},
            {"id": "alpha", "body_patterns": ["review"]},
        ]
    }
    assert resolve_skill("@theo-vane review", catalog) == Skill("alpha", None)


def test_a_tie_keeps_an_earlier_skill_whose_id_is_already_lower() -> None:
    # The earlier candidate keeps its lead when the later one matches equally
    # well but has the higher id.
    catalog = {
        "skills": [
            {"id": "alpha", "body_patterns": ["review"]},
            {"id": "zeta", "body_patterns": ["review"]},
        ]
    }
    assert resolve_skill("@theo-vane review", catalog) == Skill("alpha", None)


def test_a_first_line_that_is_only_a_handle_has_no_word_for_a_command_prefix() -> None:
    # Removing the handle can leave no first word at all, so a command prefix
    # has nothing to be compared against.
    catalog = {"skills": [{"id": "review", "command_prefixes": ["review"]}]}
    assert resolve_skill("@theo-vane", catalog) is None


def test_quotes_code_and_handles_are_ignored() -> None:
    # A command shown as code is an example; a quoted command is a citation.
    assert resolve_skill("@theo-vane ` /review ` please", CATALOG) is None
    assert resolve_skill("> @theo-vane /review\nplease have a look", CATALOG) is None


def test_a_word_boundary_keeps_ptal_out_of_capital() -> None:
    catalog = {"skills": [{"id": "ptal", "body_patterns": ["ptal"]}]}
    assert resolve_skill("@theo-vane capital", catalog) is None
    assert resolve_skill("@theo-vane ptal", catalog) == Skill("ptal", None)
    # A plain substring search would match this one; the token bound does not.
    inner = {"skills": [{"id": "tal", "body_patterns": ["tal"]}]}
    assert resolve_skill("@theo-vane capital", inner) is None
    assert resolve_skill("@theo-vane tal", inner) == Skill("tal", None)


def test_an_unknown_request_resolves_to_nothing() -> None:
    assert resolve_skill("@theo-vane can you look at this?", CATALOG) is None


def test_a_non_slash_command_prefix_is_matched_as_a_body_phrase() -> None:
    catalog = {"skills": [{"id": "ready", "command_prefixes": ["ready"]}]}
    # The phrase is not the first word, so only the body-pattern path can
    # satisfy these.
    assert resolve_skill("@theo-vane please get ready", catalog) == Skill(
        "ready", None
    )
    assert resolve_skill("@theo-vane all ready now", catalog) == Skill("ready", None)


@pytest.mark.parametrize("body", [None, "", 7])
def test_resolve_skill_is_none_for_an_empty_or_non_string_body(
    body: object,
) -> None:
    assert resolve_skill(body, CATALOG) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("catalog", [None, [], {"skills": 7}])
def test_resolve_skill_is_none_when_the_catalogue_is_unreadable(
    catalog: object,
) -> None:
    assert resolve_skill("@theo-vane /review", catalog) is None


def test_a_weaker_later_candidate_does_not_replace_the_best() -> None:
    # The earlier, longer pattern wins; the shorter one is evaluated and
    # rejected, so the loop continues with the best candidate unchanged.
    catalog = {
        "skills": [
            {"id": "long", "body_patterns": ["please review"]},
            {"id": "short", "body_patterns": ["review"]},
        ]
    }
    assert resolve_skill("@theo-vane please review", catalog) == Skill(
        "long", None
    )


def test_an_entry_without_a_usable_id_is_ignored() -> None:
    catalog = {
        "skills": [
            {"id": "", "body_patterns": ["review"]},
            {"id": None, "body_patterns": ["review"]},
            {"body_patterns": ["review"]},
        ]
    }
    assert resolve_skill("@theo-vane review", catalog) is None


def test_a_queue_type_that_is_not_a_non_empty_string_becomes_none() -> None:
    catalog = {
        "skills": [
            {"id": "a", "queue_type": "", "body_patterns": ["review"]},
            {"id": "b", "queue_type": 7, "body_patterns": ["review please"]},
        ]
    }
    assert resolve_skill("@theo-vane review", catalog) == Skill("a", None)
    assert resolve_skill("@theo-vane review please", catalog) == Skill("b", None)


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
        {"repository": {"full_name": "owner/name"}, "subject": {}},
        {"repository": {"full_name": "owner/name"}, "subject": {"url": 7}},
    ],
)
def test_an_unusable_notification_yields_nothing(item: object) -> None:
    assert notification_request(item) is None


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"id": "1001"}, "1001"),
        ({"id": ""}, None),
        ({"id": "12a"}, None),
        ({"id": 1001}, None),
        ({}, None),
        (None, None),
    ],
)
def test_a_thread_id_is_only_a_non_empty_string_of_digits(
    item: object, expected: str | None
) -> None:
    assert notification_thread_id(item) == expected


def test_a_thread_id_with_a_trailing_newline_is_not_usable() -> None:
    # `$` also matches before a trailing newline, so a `match`-based check would
    # accept "1001\n" and put the newline into the request path.
    assert notification_thread_id({"id": "1001\n"}) is None


def test_comment_author_is_none_when_the_comment_is_not_an_object() -> None:
    assert _comment_author(None) is None
    assert _comment_author("a comment") is None


def test_comment_author_reads_a_login_from_a_usable_user() -> None:
    assert _comment_author({"user": {"login": "alice"}}) == "alice"
    assert _comment_author({"user": "alice"}) is None


def test_mark_read_argv_uses_the_thread_id() -> None:
    assert mark_read_argv("12") == [
        "gh",
        "api",
        "-X",
        "PATCH",
        "notifications/threads/12",
    ]


def test_mark_read_argv_refuses_anything_but_digits() -> None:
    # The id goes into the request path, so a value with a slash would address
    # a different endpoint.
    with pytest.raises(ValueError):
        mark_read_argv("12a")


def test_mark_read_argv_refuses_a_trailing_newline() -> None:
    # `$` matches before a trailing newline; only a full match is a thread id.
    with pytest.raises(ValueError):
        mark_read_argv("1001\n")


# ------------------------------------------------------- trigger ids


def test_a_trigger_comment_id_is_its_last_part() -> None:
    assert trigger_comment_id("comment:owner/name:7:12") == 12
    assert trigger_comment_id("comment:owner/name:7:0") == 0


@pytest.mark.parametrize(
    "trigger",
    [
        None,
        "",
        "comment:owner/name:7:",
        "comment:owner/name:7:12a",
        "comment:owner/name:7: 12",
        "comment:owner/name:7:-1",
        "comment:owner/name:7:١٢",  # non-ASCII digits
    ],
)
def test_a_trigger_without_ascii_digits_has_no_comment_id(
    trigger: object,
) -> None:
    assert trigger_comment_id(trigger) is None


# ------------------------------------------------------------ scanning


def _runner(
    *,
    notifications: list,
    comments: list,
    private: str = "false",
    notif_rc: int = 0,
    comments_rc: int = 0,
    private_rc: int = 0,
    mark_read_rc: int = 0,
    head_sha: str = HEAD,
    head_rc: int = 0,
    comments_raw: str | None = None,
    calls: list[list[str]] | None = None,
):
    def run(argv: list[str]) -> Completed:
        if calls is not None:
            calls.append(list(argv))
        joined = " ".join(argv)
        # The PATCH URL contains "notifications" too, so it is matched first.
        if "notifications/threads/" in joined:
            return Completed(mark_read_rc, "", "")
        if "notifications" in joined:
            return Completed(notif_rc, json.dumps([notifications]), "")
        if "--jq .head.sha" in joined:
            return Completed(head_rc, head_sha, "")
        if "/comments" in joined:
            if comments_raw is not None:
                return Completed(comments_rc, comments_raw, "")
            return Completed(comments_rc, json.dumps([comments]), "")
        if ".private" in joined:
            return Completed(private_rc, private, "")
        raise AssertionError(argv)

    return run


def _notification(
    repo: str = "owner/name", number: int = 7, thread_id: str | None = "1001"
) -> dict:
    item = {
        "repository": {"full_name": repo},
        "subject": {"url": f"https://api.github.com/repos/{repo}/pulls/{number}"},
    }
    if thread_id is not None:
        item["id"] = thread_id
    return item


def _mark_read_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if "notifications/threads/" in " ".join(call)]


def _head_calls(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if "--jq .head.sha" in " ".join(call)]


def _comment(
    body: str, actor: str = "davidleomay", comment_id: int = 1
) -> dict:
    return {"id": comment_id, "body": body, "user": {"login": actor}}


def _jobs(store: Store) -> list[dict]:
    return store.rows("job")


def _requeue_into(store: Store, seen: list[Any] | None = None):
    """A `requeue` callback that persists the payload, as the command layer does."""

    def requeue(
        row_id: str, row: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        if seen is not None:
            seen.append((row, payload))
        store.write("job", "update", row_id, payload)
        return True

    return requeue


def _scan(store: Store, run, requeue=None, **overrides: Any):
    kwargs: dict[str, Any] = {
        "session_id": "s",
        "login": "theo-vane",
        "policy": POLICY,
        "catalog": CATALOG,
        "device_id": store.device_id(),
        "requeue": requeue if requeue is not None else _requeue_into(store),
    }
    kwargs.update(overrides)
    return scan_mentions(store, run, **kwargs)


def _insert(store: Store, **overrides: Any) -> dict[str, Any]:
    row = job_row(
        session_id="s",
        repo="owner/name",
        ref="7",
        job_type="pr-review",
        actor="davidleomay",
        queue_type="pr-ready",
        trigger="comment:owner/name:7:0",
        head_sha=None,
    )
    row.update(overrides)
    store.write("job", "insert", row["id"], row)
    return row


def test_an_admitted_mention_becomes_a_queued_job(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
            ),
        )
        assert len(created) == 1
        assert requeued == []
        assert skipped == 0
        rows = _jobs(store)
        assert len(rows) == 1
        assert rows[0]["state"] == "queued"
        assert rows[0]["repo"] == "owner/name"
        assert rows[0]["ref"] == "7"
        assert rows[0]["job_type"] == "pr-review"
        assert rows[0]["queue_type"] == "pr-ready"
        assert rows[0]["trigger"] == "comment:owner/name:7:1"
        assert rows[0]["head_sha"] == HEAD
        assert rows[0]["actor"] == "davidleomay"
    finally:
        store.close()


def test_a_job_id_over_the_limit_is_skipped_and_leaves_the_thread_unread(
    tmp_path: Path,
) -> None:
    # The longest legal repository with a one-digit reference and this skill id
    # produce an id over 200 characters. Every command that runs jobs drops such
    # an id, so inserting the row and marking the thread read would record a
    # request nothing ever runs.
    repo = "a" * 39 + "/" + "b" * 100
    skill_id = "x" * 42
    catalog = {
        "skills": [
            {
                "id": skill_id,
                "queue_type": "pr-ready",
                "body_patterns": ["please review"],
            }
        ]
    }
    policy = {**POLICY, "repos_allow": [repo]}
    calls: list[list[str]] = []
    store = Store(tmp_path)
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification(repo=repo)],
                comments=[_comment("@theo-vane please review")],
                calls=calls,
            ),
            policy=policy,
            catalog=catalog,
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
        assert _mark_read_calls(calls) == []
    finally:
        store.close()


def test_a_non_canonical_catalogue_id_names_no_skill(tmp_path: Path) -> None:
    # Only a lower-case id is used as the job type and as the `runner.json`
    # key, so an upper-case one would never find its own settings. It names no
    # skill, and the policy default cannot select it either.
    catalog = {
        "skills": [
            {
                "id": "PR-Review",
                "queue_type": "pr-ready",
                "body_patterns": ["please review"],
            }
        ]
    }
    assert resolve_skill("@theo-vane please review", catalog) is None
    assert default_skill(POLICY, catalog) is None
    store = Store(tmp_path)
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
            ),
            catalog=catalog,
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
    finally:
        store.close()


def test_a_padded_catalogue_id_is_ignored() -> None:
    # Surrounding spaces change the spelling too, so the entry names nothing.
    catalog = {
        "skills": [
            {
                "id": " pr-review ",
                "queue_type": "pr-ready",
                "body_patterns": ["please review"],
            }
        ]
    }
    assert resolve_skill("@theo-vane please review", catalog) is None
    assert default_skill(POLICY, catalog) is None
    assert allowed_skills(POLICY, catalog) == ([], ["pr-ready"])


def test_a_requeue_stores_the_canonical_catalogue_id(tmp_path: Path) -> None:
    # A row written under an older, non-canonical spelling is re-queued under
    # the id the catalogue names.
    store = Store(tmp_path)
    try:
        row = _insert(store, state="failed", job_type="PR-Review")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
            ),
        )
        assert created == []
        assert requeued == [row["id"]]
        assert skipped == 0
        assert store.row("job", row["id"])["job_type"] == "pr-review"
    finally:
        store.close()


def test_a_phrase_for_a_non_allowed_queue_type_is_denied_not_defaulted(
    tmp_path: Path,
) -> None:
    # Asking for a fix this instance does not run must not quietly become a
    # review.
    store = Store(tmp_path)
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please fix")],
            ),
        )
        assert created == []
        assert requeued == []
        assert skipped == 1
        assert _jobs(store) == []
    finally:
        store.close()


def test_an_unnamed_request_falls_back_to_the_allowed_default_skill(
    tmp_path: Path,
) -> None:
    # Text naming no catalogue pattern is not a denial by itself: the policy's
    # default supplies the skill, and its queue type is what admission checks.
    store = Store(tmp_path)
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane can you look at this?")],
            ),
        )
        assert len(created) == 1
        assert requeued == []
        assert skipped == 0
        row = _jobs(store)[0]
        assert row["job_type"] == "pr-review"
        assert row["queue_type"] == "pr-ready"
    finally:
        store.close()


def test_a_matched_skill_without_a_queue_type_is_skipped_unread(
    tmp_path: Path,
) -> None:
    # A skill the catalogue names but cannot admit on has no queue type for
    # `job_types_allow` to allow, so the request is denied rather than
    # defaulted, and the thread stays unread.
    store = Store(tmp_path)
    catalog = {
        "skills": [
            {
                "id": "implement-fix",
                "body_patterns": ["please fix"],
            }
        ]
    }
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please fix")],
                calls=calls,
            ),
            catalog=catalog,
        )
        assert created == []
        assert requeued == []
        assert skipped == 1
        assert _jobs(store) == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_the_same_mention_twice_is_one_job(tmp_path: Path) -> None:
    # What the identity is for: asking again must not queue the work twice.
    store = Store(tmp_path)
    try:
        run = _runner(
            notifications=[_notification()],
            comments=[_comment("@theo-vane please review")],
        )
        _scan(store, run)
        created, requeued, _ = _scan(store, run)
        assert created == []
        assert requeued == []
        assert len(_jobs(store)) == 1
    finally:
        store.close()


def test_a_handled_notification_is_marked_read(tmp_path: Path) -> None:
    # Without this the notification stays unread forever, and once prune deletes
    # the finished job row the same request is queued again.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                calls=calls,
            ),
        )
        assert len(created) == 1
    finally:
        store.close()
    assert _mark_read_calls(calls) == [
        ["gh", "api", "-X", "PATCH", "notifications/threads/1001"]
    ]


def test_a_notification_whose_job_already_exists_is_marked_read_again(
    tmp_path: Path,
) -> None:
    # The row may outlive the notification's unread state; marking it read on
    # the "already a job" branch keeps the thread from coming back.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        run = _runner(
            notifications=[_notification()],
            comments=[_comment("@theo-vane please review")],
            calls=calls,
        )
        _scan(store, run)
        created, requeued, _ = _scan(store, run)
        assert created == []
        assert requeued == []
        assert len(_jobs(store)) == 1
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 2


def test_the_bots_own_comment_is_never_a_request(tmp_path: Path) -> None:
    # The bot quotes the handle in its own status comment; taking that as a new
    # request would make it answer itself forever.
    store = Store(tmp_path)
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane please review", comment_id=1),
                    _comment(
                        "@theo-vane /fix please", actor="theo-vane", comment_id=2
                    ),
                ],
            ),
        )
        assert len(created) == 1
        assert _jobs(store)[0]["job_type"] == "pr-review"
    finally:
        store.close()


def test_only_the_bots_own_comment_queues_nothing(tmp_path: Path) -> None:
    # The bot's login is admitted, so only the own-comment filter can keep this
    # request from becoming a job.
    policy = dict(POLICY, actors_allow=["davidleomay", "theo-vane"])
    store = Store(tmp_path)
    try:
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", actor="theo-vane")],
            ),
            policy=policy,
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_denied_actor_leaves_the_notification_unread(tmp_path: Path) -> None:
    # Skips are re-evaluated on the next pass, so the thread must stay unread.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane please review", actor="a-stranger")
                ],
                calls=calls,
            ),
        )
        assert created == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_comment_without_a_mention_leaves_the_notification_unread(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("looks good to me")],
                calls=calls,
            ),
        )
        assert created == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_comment_without_an_integer_id_is_skipped(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    {
                        "id": "1",
                        "body": "@theo-vane please review",
                        "user": {"login": "davidleomay"},
                    }
                ],
            ),
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_comment_whose_id_is_a_boolean_is_skipped(tmp_path: Path) -> None:
    # `True` is an int subclass in Python, but a JSON boolean is not a comment
    # id: it cannot become a trigger, so the thread stays unread.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    {
                        "id": True,
                        "body": "@theo-vane please review",
                        "user": {"login": "davidleomay"},
                    }
                ],
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_an_unreadable_comment_list_leaves_the_notification_unread(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                comments_rc=1,
                calls=calls,
            ),
        )
        assert created == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


@pytest.mark.parametrize("thread_id", [None, "abc", 1001])
def test_a_notification_without_a_usable_thread_id_still_creates_the_job(
    tmp_path: Path, thread_id: object
) -> None:
    # An unusable id must not stop the request from being handled.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    item = _notification()
    if thread_id is None:
        item.pop("id")
    else:
        item["id"] = thread_id
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[item],
                comments=[_comment("@theo-vane please review")],
                calls=calls,
            ),
        )
        assert len(created) == 1
        assert len(_jobs(store)) == 1
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_thread_id_with_a_trailing_newline_creates_the_job_without_marking_read(
    tmp_path: Path,
) -> None:
    # An id that is not digits all the way through must not reach the request
    # path; the request is still handled.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification(thread_id="1001\n")],
                comments=[_comment("@theo-vane please review")],
                calls=calls,
            ),
        )
        assert len(created) == 1
        assert len(_jobs(store)) == 1
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_failing_mark_read_does_not_stop_the_job(tmp_path: Path) -> None:
    # The row still de-duplicates until prune, and the next pass tries again.
    store = Store(tmp_path)
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                mark_read_rc=1,
            ),
        )
        assert len(created) == 1
        assert len(_jobs(store)) == 1
    finally:
        store.close()


def test_an_actor_outside_the_policy_queues_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", actor="a-stranger")],
            ),
        )
        assert created == []
        assert skipped == 1
        assert _jobs(store) == []
    finally:
        store.close()


def test_a_comment_that_does_not_address_us_queues_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("looks good to me")],
            ),
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_private_repo_without_the_exception_queues_nothing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private="true",
            ),
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_private_repo_named_twice_is_admitted(tmp_path: Path) -> None:
    store = Store(tmp_path)
    policy = dict(POLICY, agent_identity={"private_repos_allow": ["owner/name"]})
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private="true",
            ),
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
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private_rc=1,
            ),
        )
        assert created == []
        assert skipped == 1
    finally:
        store.close()


def test_a_failing_notification_listing_is_not_an_empty_queue(
    tmp_path: Path,
) -> None:
    # The list is the input to every admission decision. A failed call must be
    # reported, never read as "nothing to do".
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        with pytest.raises(ListingFailed):
            _scan(
                store,
                _runner(
                    notifications=[_notification()],
                    comments=[_comment("@theo-vane please review")],
                    notif_rc=1,
                    calls=calls,
                ),
            )
        assert _jobs(store) == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_failing_comment_listing_queues_nothing_and_skips(
    tmp_path: Path,
) -> None:
    # One unreadable thread is a skip; the rest of the listing is still a
    # usable input, so this is not a failed pass.
    store = Store(tmp_path)
    try:
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                comments_rc=1,
            ),
        )
        assert created == []
        assert skipped == 1
        assert _jobs(store) == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "not json",
        "{}",
        '"a string"',
        "[[], {}]",
        "[[], 7]",
    ],
)
def test_a_notification_listing_that_is_not_a_list_of_pages_fails(
    tmp_path: Path, stdout: str
) -> None:
    # `--slurp` promises one list per page. Anything else means the output
    # cannot be trusted, so no thread is admitted or marked read.
    store = Store(tmp_path)
    calls: list[list[str]] = []

    def run(argv: list[str]) -> Completed:
        calls.append(list(argv))
        return Completed(0, stdout, "")

    try:
        with pytest.raises(ListingFailed):
            _scan(store, run)
        assert _jobs(store) == []
    finally:
        store.close()
    # Only the listing was asked for; nothing was read or marked.
    assert calls == [["gh", "api", "--paginate", "--slurp", "notifications"]]


def test_the_newest_addressing_comment_decides(tmp_path: Path) -> None:
    # An older mention asking for something else must not override the latest one.
    store = Store(tmp_path)
    try:
        created, _, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane /fix please", comment_id=1),
                    _comment("@theo-vane please review", comment_id=2),
                ],
            ),
        )
        assert len(created) == 1
        assert _jobs(store)[0]["job_type"] == "pr-review"
        assert _jobs(store)[0]["trigger"] == "comment:owner/name:7:2"
    finally:
        store.close()


def test_a_fresh_row_starts_queued_with_no_attempts() -> None:
    row = job_row(
        session_id="s",
        repo="owner/name",
        ref="7",
        job_type="pr-review",
        actor="a",
        queue_type="pr-ready",
        trigger="comment:owner/name:7:1",
        head_sha=HEAD,
    )
    assert row["state"] == "queued"
    assert row["attempts"] == 0
    assert row["created_at"] == row["updated_at"]
    assert row["queue_type"] == "pr-ready"
    assert row["trigger"] == "comment:owner/name:7:1"
    assert row["head_sha"] == HEAD


def test_a_notification_that_names_no_target_is_skipped(tmp_path: Path) -> None:
    # A notification without a usable repository and subject cannot be turned
    # into a request, so it is skipped before any thread is read.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(notifications=[{}], comments=[], calls=calls),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
    finally:
        store.close()
    assert calls == [["gh", "api", "--paginate", "--slurp", "notifications"]]


def test_a_comment_listing_that_is_not_json_is_skipped(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[],
                comments_raw="{not json",
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


@pytest.mark.parametrize(
    "stdout",
    [
        "null",
        "5",
        "true",
        "{}",
        json.dumps([[_comment("@theo-vane please review")], {}]),
    ],
)
def test_a_comment_listing_that_is_not_a_list_of_pages_is_skipped(
    tmp_path: Path, stdout: str
) -> None:
    # `--slurp` promises one list per page. Anything else cannot be trusted,
    # so the thread stays unread and no row is written.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[],
                comments_raw=stdout,
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_comment_page_element_that_is_not_an_object_is_ignored(
    tmp_path: Path,
) -> None:
    # A page is a list; a value in it that is not a comment object names no
    # comment, so the one real comment still decides the request.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[],
                comments_raw=json.dumps(
                    [[_comment("@theo-vane please review"), "x"]]
                ),
                calls=calls,
            ),
        )
        assert len(created) == 1
        assert requeued == []
        assert skipped == 0
        assert _jobs(store)[0]["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


@pytest.mark.parametrize("user", [{"login": 7}, {"login": ""}, "alice"])
def test_a_comment_without_a_usable_author_is_skipped(
    tmp_path: Path, user: object
) -> None:
    # The login becomes the recorded actor; a missing or non-string one cannot
    # be admitted, so the thread stays unread.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    {
                        "id": 1,
                        "body": "@theo-vane please review",
                        "user": user,
                    }
                ],
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert _jobs(store) == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_an_unreadable_head_still_creates_the_job_with_no_head(
    tmp_path: Path,
) -> None:
    # An unknown head is the fail-closed answer: the job is still created, but
    # it records no head that a later pass could mistake for a known one.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                head_rc=1,
                calls=calls,
            ),
        )
        assert len(created) == 1
        assert requeued == []
        assert skipped == 0
        saved = _jobs(store)
        assert len(saved) == 1
        assert saved[0]["head_sha"] is None
    finally:
        store.close()
    assert len(_head_calls(calls)) == 1
    assert len(_mark_read_calls(calls)) == 1


def test_a_row_in_an_unexpected_state_is_skipped(tmp_path: Path) -> None:
    # Only `failed` and `done` are decided further; any other stored state is
    # not a request this pass may act on.
    store = Store(tmp_path)
    seen: list[Any] = []
    calls: list[list[str]] = []
    try:
        row = _insert(store, state="cancelled")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert seen == []
        assert store.row("job", row["id"])["state"] == "cancelled"
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_row_stored_under_the_key_with_another_id_is_skipped(
    tmp_path: Path,
) -> None:
    # The cleanup that follows a re-queue uses the stored id as a path. A row
    # whose id is not the key it was looked up by is refused before any
    # decision, and the thread stays unread for a pass that sees a consistent
    # row.
    store = Store(tmp_path)
    seen: list[Any] = []
    calls: list[list[str]] = []
    try:
        row = _insert(store, state="failed")
        mismatched = {k: v for k, v in row.items() if k != "id"}
        mismatched["id"] = "another-job"
        store.write("job", "update", row["id"], mismatched)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert seen == []
        stored = store.row("job", row["id"])
        assert stored is not None
        assert stored["id"] == "another-job"
        assert stored["state"] == "failed"
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_failed_row_whose_id_cannot_be_read_is_skipped(
    tmp_path: Path,
) -> None:
    # `retry_payload` needs the stored id; without a usable one the row cannot
    # be re-queued, so the mention stays unread for a later pass.
    store = Store(tmp_path)
    seen: list[Any] = []
    try:
        row = _insert(store, state="failed")
        payload = {k: v for k, v in row.items() if k != "id"}
        store.write("job", "update", row["id"], payload)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert seen == []
    finally:
        store.close()


def test_a_done_job_whose_duplicate_cannot_be_recorded_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unchanged head is a duplicate. If recording that comment fails, the
    # thread stays unread and the pass reports a skip.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        _insert(
            store,
            state="done",
            head_sha=HEAD,
            trigger="comment:owner/name:7:1",
        )

        def fail_write(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(store, "write", fail_write)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
                head_sha=HEAD,
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_done_row_whose_id_cannot_be_read_is_skipped_when_the_head_moved(
    tmp_path: Path,
) -> None:
    # The head moved, but a row without an id cannot be re-queued; the pass
    # skips it instead of inventing an identity.
    store = Store(tmp_path)
    seen: list[Any] = []
    try:
        row = _insert(store, state="done", head_sha=OTHER_HEAD)
        payload = {k: v for k, v in row.items() if k != "id"}
        store.write("job", "update", row["id"], payload)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_sha=HEAD,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert seen == []
    finally:
        store.close()


def test_an_unparseable_stored_trigger_whose_write_fails_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row with no parseable trigger is consumed rather than retried. If that
    # record fails, the mention is skipped and the thread stays unread.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        _insert(
            store,
            state="failed",
            trigger="comment:owner/name:7:not-an-id",
        )

        def fail_write(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(store, "write", fail_write)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


# ------------------------------------------------- existing rows


def test_a_queued_job_is_not_requeued_but_marked_read(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="queued")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert skipped == 0
        assert seen == []
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


def test_a_running_job_is_not_requeued_but_marked_read(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="running")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert skipped == 0
        assert seen == []
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


def test_a_running_job_records_a_new_mention_so_it_is_not_replayed(
    tmp_path: Path,
) -> None:
    # A handled mention that does not start a job is still consumed: once the
    # job fails and the thread is unread again, the same comment must select
    # nothing, while a newer comment still retries the job.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="running", trigger="comment:owner/name:7:1")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:2"

        stored = store.row("job", row["id"])
        assert stored is not None
        stored = {k: v for k, v in stored.items() if not k.startswith("_")}
        stored["state"] = "failed"
        store.write("job", "update", row["id"], stored)

        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert seen == []

        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane please review", comment_id=2),
                    _comment("@theo-vane please review", comment_id=3),
                ],
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [row["id"]], 0)
        _old, payload = seen[0]
        assert payload["trigger"] == "comment:owner/name:7:3"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


def test_the_same_trigger_is_already_handled(tmp_path: Path) -> None:
    # Even a failed row is not retried for the very comment that created it.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        _insert(
            store,
            state="failed",
            trigger="comment:owner/name:7:1",
        )
        created, requeued, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert seen == []
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


def test_an_earlier_comment_does_not_revive_a_failed_job(
    tmp_path: Path,
) -> None:
    # C1 created the job, C2 was consumed while it ran, and the job then failed.
    # If C2 is deleted or edited, C1 is the latest addressing comment again; it
    # is still already handled and must not retry the job.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="running", trigger="comment:owner/name:7:1")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane please review", comment_id=1),
                    _comment("@theo-vane please review", comment_id=2),
                ],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:2"

        stored = store.row("job", row["id"])
        assert stored is not None
        stored = {k: v for k, v in stored.items() if not k.startswith("_")}
        stored["state"] = "failed"
        store.write("job", "update", row["id"], stored)
        before = store.row("job", row["id"])

        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert seen == []
        assert store.row("job", row["id"]) == before
        assert len(_mark_read_calls(calls)) == 2

        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[
                    _comment("@theo-vane please review", comment_id=1),
                    _comment("@theo-vane please review", comment_id=3),
                ],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [row["id"]], 0)
        _old, payload = seen[0]
        assert payload["trigger"] == "comment:owner/name:7:3"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 3


def test_a_stored_trigger_that_does_not_parse_is_recorded(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    seen: list[Any] = []
    try:
        row = _insert(
            store, state="failed", trigger="comment:owner/name:7:not-an-id"
        )
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert seen == []
        saved = store.row("job", row["id"])
        assert saved["state"] == "failed"
        assert saved["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()


def test_a_row_without_a_trigger_is_treated_as_handled(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="failed", trigger=None)
        created, requeued, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert seen == []
        saved = store.row("job", row["id"])
        assert saved["state"] == "failed"
        assert saved["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


def test_a_failed_job_is_requeued_for_a_new_trigger(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="failed", attempts=2)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == [row["id"]]
        assert skipped == 0
        assert len(seen) == 1
        _old, payload = seen[0]
        assert payload["state"] == "queued"
        assert payload["attempts"] == 3
        assert payload["trigger"] == "comment:owner/name:7:1"
        assert payload["actor"] == "davidleomay"
        assert payload["queue_type"] == "pr-ready"
        assert store.row("job", row["id"])["attempts"] == 3
    finally:
        store.close()
    # No head is needed to decide a failed row.
    assert _head_calls(calls) == []
    assert len(_mark_read_calls(calls)) == 1


def test_a_done_job_is_queued_again_when_the_head_moved(tmp_path: Path) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(
            store,
            state="done",
            attempts=1,
            head_sha=OTHER_HEAD,
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-01T00:00:00Z",
        )
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_sha=HEAD,
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
            session_id="session-2",
        )
        assert created == []
        assert requeued == [row["id"]]
        assert skipped == 0
        _old, payload = seen[0]
        assert payload["state"] == "queued"
        assert payload["attempts"] == 0
        assert payload["head_sha"] == HEAD
        assert payload["trigger"] == "comment:owner/name:7:1"
        assert payload["session_id"] == "session-2"
        assert payload["created_at"] != "2020-01-01T00:00:00Z"
        assert payload["created_at"] == payload["updated_at"]
        saved = store.row("job", row["id"])
        assert saved["attempts"] == 0
        assert saved["created_at"] == payload["created_at"]
    finally:
        store.close()
    assert len(_head_calls(calls)) == 1
    assert len(_mark_read_calls(calls)) == 1


def test_a_new_generation_waits_when_its_created_at_is_not_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Status activity ids and the edit marker derive from created_at +
    # attempts, and utcnow() has second precision. Re-queueing with the same
    # created_at would reuse the previous generation's identity, so this pass
    # defers the mention: no re-queue, and the thread stays unread.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(
            store,
            state="done",
            attempts=1,
            head_sha=OTHER_HEAD,
            created_at="2020-01-01T00:00:00Z",
            updated_at="2020-01-01T00:00:00Z",
        )
        monkeypatch.setattr(
            "agent_cli.ingest.utcnow", lambda: "2020-01-01T00:00:00Z"
        )
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_sha=HEAD,
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert skipped == 1
        assert seen == []
        assert store.row("job", row["id"])["state"] == "done"
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


@pytest.mark.parametrize(
    "overrides,head_sha",
    [
        ({"head_sha": HEAD}, HEAD),
        ({"head_sha": None}, HEAD),
    ],
)
def test_a_done_job_is_a_duplicate_when_the_head_did_not_move(
    tmp_path: Path, overrides: dict[str, Any], head_sha: str
) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="done", **overrides)
        created, requeued, _ = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_sha=head_sha,
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert seen == []
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


@pytest.mark.parametrize("stored", ["", "not-a-commit", "A" * 40])
def test_a_done_job_with_a_malformed_stored_head_is_a_duplicate(
    tmp_path: Path, stored: str
) -> None:
    # A stored head that is not a commit cannot show that the head moved, so
    # the mention is consumed as a duplicate instead of starting a new
    # generation and discarding the previous evidence.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="done", attempts=1, head_sha=stored)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_sha=HEAD,
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert seen == []
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


@pytest.mark.parametrize(
    "answer,returncode",
    [
        # A malformed answer and a failed call both leave the head unknown.
        ("", 0),
        (HEAD, 1),
    ],
)
def test_a_done_job_with_a_stored_head_defers_an_unreadable_current_head(
    tmp_path: Path, answer: str, returncode: int
) -> None:
    # The head is unknown, not unchanged: consuming the mention would discard
    # a real request after a rate limit or a network error.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="done", attempts=1, head_sha=OTHER_HEAD)
        before = store.row("job", row["id"])
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_sha=answer,
                head_rc=returncode,
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 1)
        assert seen == []
        assert store.row("job", row["id"]) == before
    finally:
        store.close()
    assert len(_head_calls(calls)) == 1
    assert _mark_read_calls(calls) == []


@pytest.mark.parametrize("stored", [None, "not-a-commit", ""])
def test_a_done_job_without_a_stored_commit_id_consumes_an_unreadable_head(
    tmp_path: Path, stored: str | None
) -> None:
    # A row that does not store a commit id (an issue job, or one from before
    # heads were stored) keeps the duplicate path even when the head is
    # unreadable.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    try:
        row = _insert(store, state="done", attempts=1, head_sha=stored)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                head_rc=1,
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert seen == []
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:1"
    finally:
        store.close()
    assert len(_mark_read_calls(calls)) == 1


def test_a_done_job_consumes_a_mention_that_did_not_move_the_head(
    tmp_path: Path,
) -> None:
    # The unchanged-head duplicate records the comment. When the head moves
    # later, that same comment is already handled rather than a new generation.
    store = Store(tmp_path)
    seen: list[Any] = []
    try:
        row = _insert(
            store,
            state="done",
            attempts=1,
            head_sha=HEAD,
            trigger="comment:owner/name:7:1",
        )
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
                head_sha=HEAD,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert store.row("job", row["id"])["trigger"] == "comment:owner/name:7:2"

        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
                head_sha=OTHER_HEAD,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert (created, requeued, skipped) == ([], [], 0)
        assert seen == []
    finally:
        store.close()


def test_a_failed_trigger_write_skips_and_leaves_the_thread_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        _insert(store, state="running", trigger="comment:owner/name:7:1")

        def fail_write(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(store, "write", fail_write)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_store_error_from_the_trigger_write_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A StoreError is a genuine data problem, not a retryable one. It must not
    # be reported as a skip, which would hide it forever.
    store = Store(tmp_path)
    try:
        _insert(store, state="running", trigger="comment:owner/name:7:1")

        def fail_write(*_args: Any, **_kwargs: Any) -> None:
            raise StoreError("boom")

        monkeypatch.setattr(store, "write", fail_write)
        with pytest.raises(StoreError):
            _scan(
                store,
                _runner(
                    notifications=[_notification()],
                    comments=[
                        _comment("@theo-vane please review", comment_id=2)
                    ],
                ),
            )
    finally:
        store.close()


def test_a_store_connection_error_from_the_trigger_write_is_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lost connection is retryable: the mention is skipped and the thread
    # stays unread so the next pass tries again.
    store = Store(tmp_path)
    calls: list[list[str]] = []
    try:
        _insert(store, state="running", trigger="comment:owner/name:7:1")

        def fail_write(*_args: Any, **_kwargs: Any) -> None:
            raise StoreConnectionError("lost")

        monkeypatch.setattr(store, "write", fail_write)
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=2)],
                calls=calls,
            ),
        )
        assert (created, requeued, skipped) == ([], [], 1)
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


def test_a_foreign_job_is_skipped_and_the_thread_stays_unread(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []
    seen: list[Any] = []
    row = job_row(
        session_id="s",
        repo="owner/name",
        ref="7",
        job_type="pr-review",
        actor="davidleomay",
        trigger="comment:owner/name:7:99",
    )
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "job",
                "op": "insert",
                "row_id": row["id"],
                "payload": row,
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=_requeue_into(store, seen),
        )
        assert created == []
        assert requeued == []
        assert skipped == 1
        assert seen == []
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


@pytest.mark.parametrize("outcome", [False, "raise"])
def test_a_refused_requeue_leaves_the_thread_unread(
    tmp_path: Path, outcome: object
) -> None:
    store = Store(tmp_path)
    calls: list[list[str]] = []

    def requeue(
        _row_id: str, _row: dict[str, Any], _payload: dict[str, Any]
    ) -> bool:
        if outcome == "raise":
            raise RuntimeError("boom")
        return False

    try:
        _insert(store, state="failed")
        created, requeued, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review", comment_id=1)],
                calls=calls,
            ),
            requeue=requeue,
        )
        assert created == []
        assert requeued == []
        assert skipped == 1
    finally:
        store.close()
    assert _mark_read_calls(calls) == []


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
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane please review")],
                private="null",
            ),
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
        created, _, skipped = _scan(
            store,
            _runner(
                notifications=[_notification()],
                comments=[_comment("@theo-vane can you look at this?")],
            ),
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
    assert not mentions("write ~~~\n@theo-vane /review\n~~~ to ask", "theo-vane")
    assert not mentions("the handle is `@theo-vane`, use it in a comment", "theo-vane")
    # A real request alongside an example still counts.
    assert mentions("`@theo-vane` is the handle — @theo-vane please review", "theo-vane")


def test_a_request_named_only_inside_code_does_not_resolve() -> None:
    # The code names a different skill than the running text does, by a longer
    # pattern, so reading the code would change the result.
    catalog = {
        "skills": [
            {"id": "review", "body_patterns": ["please review"]},
            {"id": "fix", "body_patterns": ["please fix this right now"]},
        ]
    }
    body = "example `please fix this right now` — @theo-vane please review"
    assert resolve_skill(body, catalog) == Skill("review", None)


def test_a_longer_pattern_is_not_matched_by_its_prefix() -> None:
    # A plain substring search would find `review` inside `review-deep`; the
    # token bound keeps the longer request distinct.
    catalog = {
        "skills": [
            {"id": "short", "body_patterns": ["review"]},
            {"id": "deep", "body_patterns": ["review-deep"]},
        ]
    }
    assert resolve_skill("@x review-deep please", catalog) == Skill("deep", None)
    assert resolve_skill("@x review please", catalog) == Skill("short", None)
    # With only the shorter pattern configured, the hyphen stays part of the
    # token and `review-deep` does not name it.
    short_only = {"skills": [{"id": "short", "body_patterns": ["review"]}]}
    assert resolve_skill("@x review-deep please", short_only) is None


def test_an_email_address_whose_domain_begins_with_the_handle_is_not_an_address() -> None:
    # A mailbox at a domain that starts with this login is not a mention of it:
    # the `@` sits at the end of a longer word, the local part.
    assert not mentions("write to support@theo-vane.example", "theo-vane")


def test_a_handle_preceded_directly_by_a_word_character_is_not_an_address() -> None:
    # The same shape without the rest of an e-mail: a letter glued to the `@`
    # means the handle is not a token of its own.
    assert not mentions("see x@theo-vane please review", "theo-vane")


def test_a_real_mention_in_running_text_still_addresses_us() -> None:
    # A space is outside `[a-z0-9-]`, so a genuine mention still matches once
    # the leading bound is in place.
    assert mentions("please review @theo-vane thanks", "theo-vane")
