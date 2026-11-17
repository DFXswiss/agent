"""Tests for runner_status: the announcement and outcome comments.

The store is the real PostgreSQL store the rest of the suite uses. GitHub is
faked at `github_accounts.load_accounts` and at the injected activity executor,
so no `gh` runs and the assertions are about the row this module writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_cli import github_accounts, runner_status
from agent_cli.ingest import job_row
from agent_cli.runtime import Completed
from agent_cli.store import Store


# --------------------------------------------------------------------- fakes


class _FakeAccounts:
    def __init__(self, accounts: dict[str, github_accounts.Account]) -> None:
        self._accounts = accounts

    def for_session(self, session_id: str) -> github_accounts.Account:
        account = self._accounts.get(session_id)
        if account is None:
            raise github_accounts.AccountError(
                f"No GitHub account configured for session {session_id}"
            )
        return account


def _install_account(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login: str = "example-bot",
    present: bool = True,
) -> github_accounts.Account:
    account = github_accounts.Account(
        name="test", login=login, gh_config_dir="/tmp/gh"
    )
    accounts = {"s": account} if present else {}
    monkeypatch.setattr(
        runner_status.github_accounts,
        "load_accounts",
        lambda _home: _FakeAccounts(accounts),
    )
    return account


def _runner(argv: list[str]) -> Completed:
    raise AssertionError(argv)


def _done_executor(seen: list[dict[str, Any]] | None = None) -> Any:
    def execute(store: Store, _runner: Any, activity_id: str) -> str:
        row = store.row("activity", activity_id)
        assert isinstance(row, dict)
        if seen is not None:
            seen.append(row)
        saved = {key: value for key, value in row.items() if not key.startswith("_")}
        saved["execution_status"] = "done"
        store.write("activity", "update", activity_id, saved)
        return f"comment.post {activity_id} done"

    return execute


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "job-1",
        "session_id": "s",
        "repo": "owner/name",
        "ref": "7",
        "pull_request": False,
        "attempts": 0,
    }
    row.update(overrides)
    return row


def _post(
    store: Store,
    row: Any,
    *,
    home: Path,
    kind: str = "announce",
    body: str = "Working on this now.",
    expected_login: str = "example-bot",
    execute: Any = None,
) -> bool:
    return runner_status.post_status(
        store,
        _runner,
        row=row,
        kind=kind,
        body=body,
        home=home,
        expected_login=expected_login,
        execute_activity=execute or _done_executor(),
    )


def _announce(
    store: Store,
    row: Any,
    *,
    home: Path,
    body: str = "Working on this now.",
    expected_login: str = "example-bot",
    execute: Any = None,
) -> bool | None:
    return runner_status.announce_status(
        store,
        _runner,
        row=row,
        body=body,
        home=home,
        expected_login=expected_login,
        execute_activity=execute or _done_executor(),
    )


# --------------------------------------------------------------------- bodies


def test_announce_body_names_the_job_in_both_languages() -> None:
    body = runner_status.announce_body("job-1", 0)

    assert body.startswith("Working on this now: job `job-1`.\n\n")
    assert "<details><summary>DE</summary>" in body
    assert "Ich arbeite jetzt daran: Auftrag `job-1`." in body
    assert "attempt" not in body


@pytest.mark.parametrize("attempts", [1, 4])
def test_announce_body_numbers_a_retry_in_both_languages(attempts: int) -> None:
    # `attempts` counts the finished attempts, so the comment names the one
    # that is starting now.
    body = runner_status.announce_body("job-1", attempts)

    assert f"job `job-1` (attempt {attempts + 1})." in body
    assert f"Auftrag `job-1` (Versuch {attempts + 1})." in body


@pytest.mark.parametrize(
    ("outcome", "exit_code", "minutes", "english", "german"),
    [
        (
            "agent_failed",
            1,
            None,
            "Job `job-1` ended with exit code 1.",
            "Auftrag `job-1` endete mit Fehlercode 1.",
        ),
        (
            "agent_failed",
            0,
            None,
            "Job `job-1` ended without publishing a result.",
            "ohne ein Ergebnis zu veröffentlichen.",
        ),
        (
            "agent_failed",
            None,
            None,
            "Job `job-1` ended without publishing a result.",
            "ohne ein Ergebnis zu veröffentlichen.",
        ),
        (
            "timeout",
            None,
            30,
            "Job `job-1` exceeded its time limit of 30 minutes",
            "Zeitlimit von 30 Minuten",
        ),
        (
            "timeout",
            None,
            None,
            "Job `job-1` exceeded its time limit and was stopped.",
            "Zeitlimit überschritten und wurde beendet.",
        ),
        (
            "stalled",
            None,
            5,
            "showed no progress for 5 minutes",
            "5 Minuten keinen Fortschritt",
        ),
        (
            "stalled",
            None,
            None,
            "Job `job-1` stopped making progress and was stopped.",
            "keinen Fortschritt mehr gezeigt und wurde beendet.",
        ),
        (
            "crashed",
            None,
            None,
            "Job `job-1` stopped unexpectedly.",
            "ist unerwartet abgebrochen.",
        ),
        (
            "workspace_failed",
            None,
            None,
            "the working copy could not be prepared.",
            "Die Arbeitskopie konnte nicht hergestellt werden.",
        ),
    ],
)
def test_outcome_body_describes_each_failure_in_both_languages(
    outcome: str,
    exit_code: int | None,
    minutes: int | None,
    english: str,
    german: str,
) -> None:
    body = runner_status.outcome_body(
        "job-1", outcome, exit_code=exit_code, minutes=minutes
    )

    assert body is not None
    assert english in body
    assert german in body
    assert "<details><summary>DE</summary>" in body


@pytest.mark.parametrize("outcome", ["success", "mystery"])
def test_outcome_body_is_none_when_no_comment_is_owed(outcome: str) -> None:
    # A successful job's posted review is its output; an unknown outcome has
    # no wording that would be true.
    assert (
        runner_status.outcome_body(
            "job-1", outcome, exit_code=0, minutes=30
        )
        is None
    )


# ------------------------------------------------------------- activity ids


def test_status_activity_id_is_deterministic() -> None:
    assert runner_status.status_activity_id(
        "announce", "job-1", 0
    ) == runner_status.status_activity_id("announce", "job-1", 0)


def test_status_activity_id_differs_by_kind_and_attempt() -> None:
    ids = {
        runner_status.status_activity_id(kind, "job-1", attempts)
        for kind in ("announce", "outcome")
        for attempts in (0, 1)
    }

    assert len(ids) == 4


def test_status_activity_id_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="announce or outcome"):
        runner_status.status_activity_id("other", "job-1", 0)


# ---------------------------------------------------------- queue_activity


def _account() -> github_accounts.Account:
    return github_accounts.Account(
        name="test", login="Example-Bot", gh_config_dir="/tmp/gh"
    )


def _queue(
    store: Store,
    *,
    activity_id: str = "a-1",
    payload: dict[str, Any] | None = None,
    account: github_accounts.Account | None = None,
) -> bool:
    return runner_status.queue_activity(
        store,
        activity_id=activity_id,
        session_id="s",
        activity_type="comment.post",
        payload=payload
        or {"repo": "owner/name", "number": 7, "body": "x", "target": "issue"},
        account=account or _account(),
    )


def test_queue_activity_inserts_a_pending_row_with_the_account_pinned(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    try:
        assert _queue(store) is True
        row = store.row("activity", "a-1")
        assert row is not None
        assert row["execution_status"] == "pending"
        assert row["session_id"] == "s"
        assert row["type"] == "comment.post"
        assert row["payload"] == {
            "repo": "owner/name",
            "number": 7,
            "body": "x",
            "target": "issue",
        }
        assert row["execution_account"] == {
            "account": "test",
            "login": "example-bot",
        }
    finally:
        store.close()


def test_queue_activity_returns_false_for_a_done_row_without_writing(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "a-1",
            {
                "id": "a-1",
                "session_id": "s",
                "type": "comment.post",
                "execution_status": "done",
                "payload": {"body": "previous"},
            },
        )

        assert _queue(store) is False
        row = store.row("activity", "a-1")
        assert row is not None
        assert row["execution_status"] == "done"
        assert row["payload"] == {"body": "previous"}
    finally:
        store.close()


def test_queue_activity_reopens_a_failed_row_and_keeps_its_account(
    tmp_path: Path,
) -> None:
    # A retry executes the activity again but must not be able to switch the
    # account that posts the comment.
    store = Store(tmp_path)
    try:
        store.write(
            "activity",
            "insert",
            "a-1",
            {
                "id": "a-1",
                "session_id": "s",
                "type": "comment.post",
                "execution_status": "error",
                "payload": {"body": "old"},
                "execution_account": {"account": "old", "login": "old-bot"},
            },
        )

        assert _queue(store, payload={"body": "new"}) is True
        row = store.row("activity", "a-1")
        assert row is not None
        assert row["execution_status"] == "pending"
        assert row["payload"] == {"body": "new"}
        assert row["execution_account"] == {"account": "old", "login": "old-bot"}
    finally:
        store.close()


def test_queue_activity_refuses_a_row_from_another_device(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "activity",
                "op": "insert",
                "row_id": "a-1",
                "payload": {"id": "a-1", "execution_status": "error"},
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )

        with pytest.raises(ValueError, match="another device"):
            _queue(store)
    finally:
        store.close()


# ---------------------------------------------------------------- post_status


@pytest.mark.parametrize(
    ("pull_request", "target"), [(True, "pr"), (False, "issue")]
)
def test_post_status_posts_against_the_recorded_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pull_request: bool,
    target: str,
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    seen: list[dict[str, Any]] = []
    try:
        posted = _post(
            store,
            _row(pull_request=pull_request),
            home=tmp_path,
            execute=_done_executor(seen),
        )
    finally:
        store.close()

    assert posted is True
    assert len(seen) == 1
    assert seen[0]["payload"]["target"] == target
    assert seen[0]["payload"]["number"] == 7


@pytest.mark.parametrize(
    "row",
    [
        None,
        _row(session_id=None),
        _row(session_id=7),
        _row(repo=""),
        _row(repo=7),
        _row(ref="abc"),
        _row(ref="0"),
        _row(ref="#-1"),
        _row(ref=7),
        _row(pull_request=None),
        _row(pull_request="true"),
        _row(attempts=None),
        _row(attempts=True),
        _row(id=""),
        _row(id=7),
    ],
)
def test_post_status_fails_closed_for_an_unusable_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: Any
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    try:
        assert _post(store, row, home=tmp_path) is False
        assert store.rows("activity") == []
    finally:
        store.close()


def test_post_status_fails_closed_without_an_account_for_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch, present=False)
    store = Store(tmp_path)
    try:
        assert _post(store, _row(), home=tmp_path) is False
        assert store.rows("activity") == []
    finally:
        store.close()


def test_post_status_fails_closed_when_the_account_is_not_the_expected_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Posting as another login would attribute the comment to the wrong user.
    _install_account(monkeypatch, login="someone-else")
    store = Store(tmp_path)
    try:
        assert _post(store, _row(), home=tmp_path) is False
        assert store.rows("activity") == []
    finally:
        store.close()


def test_post_status_fails_closed_when_queueing_the_activity_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)

    def raising_queue(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(runner_status, "queue_activity", raising_queue)
    try:
        assert _post(store, _row(), home=tmp_path) is False
    finally:
        store.close()


def test_post_status_fails_closed_when_the_executor_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)

    def raising_execute(*_args: Any) -> str:
        raise RuntimeError("gh unavailable")

    try:
        assert _post(store, _row(), home=tmp_path, execute=raising_execute) is False
    finally:
        store.close()


def test_post_status_returns_false_when_the_activity_did_not_end_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)

    def unfinished_execute(_store: Store, _runner: Any, _activity_id: str) -> str:
        return "comment.post pending"

    try:
        assert _post(store, _row(), home=tmp_path, execute=unfinished_execute) is False
    finally:
        store.close()


def test_post_status_retires_an_announcement_whose_executor_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row left pending would be posted by a later scan, after the output
    # baseline, and would be counted as the job's own output.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = runner_status.status_activity_id("announce", "job-1", 0)

    def raising_execute(*_args: Any) -> str:
        raise RuntimeError("gh unavailable")

    try:
        assert _post(store, _row(), home=tmp_path, execute=raising_execute) is False
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "error"
    assert saved["error"] == "announcement not posted before the job started"


def test_post_status_retires_an_announcement_left_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = runner_status.status_activity_id("announce", "job-1", 0)

    def unfinished_execute(_store: Store, _runner: Any, _activity_id: str) -> str:
        return "comment.post pending"

    try:
        assert _post(store, _row(), home=tmp_path, execute=unfinished_execute) is False
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "error"
    assert saved["error"] == "announcement not posted before the job started"


def test_post_status_leaves_a_done_announcement_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The comment exists and was posted before the baseline, so the row needs
    # no retirement and the announcement counts as delivered.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = runner_status.status_activity_id("announce", "job-1", 0)

    try:
        assert _post(store, _row(), home=tmp_path, execute=_done_executor()) is True
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "done"
    assert "error" not in saved


def _insert_pending_announcement(store: Store) -> str:
    """Leave this attempt's announcement pending, as an interrupted dispatch does."""
    activity_id = runner_status.status_activity_id("announce", "job-1", 0)
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": "s",
            "type": "comment.post",
            "execution_status": "pending",
            "payload": {"body": "previous"},
        },
    )
    return activity_id


def test_announce_status_retires_a_leftover_row_when_queueing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An earlier interrupted dispatch left the row pending. Queueing cannot
    # re-open it, so the False from `_post_status` must still retire the row or
    # a later scan posts the old announcement after the baseline.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = _insert_pending_announcement(store)
    original_write = store.write
    updates = 0

    def write(table: str, op: str, row_id: str, payload: dict[str, Any]) -> None:
        nonlocal updates
        if table == "activity" and op == "update":
            updates += 1
            if updates == 1:
                raise RuntimeError("db unavailable")
        original_write(table, op, row_id, payload)

    monkeypatch.setattr(store, "write", write)
    try:
        assert _announce(store, _row(), home=tmp_path) is False
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "error"
    assert saved["error"] == "announcement not posted before the job started"


def test_announce_status_retires_a_leftover_row_without_an_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Validation rejects the post before it looks at the activity, but the
    # leftover row can still be executed by a later scan.
    _install_account(monkeypatch, present=False)
    store = Store(tmp_path)
    activity_id = _insert_pending_announcement(store)
    try:
        assert _announce(store, _row(), home=tmp_path) is False
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "error"
    assert saved["error"] == "announcement not posted before the job started"


def test_announce_status_reports_unsafe_when_every_update_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither queueing nor the retirement write can touch the row, so it stays
    # pending and a later scan may still post it. Dispatch must not start.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = _insert_pending_announcement(store)
    original_write = store.write

    def write(table: str, op: str, row_id: str, payload: dict[str, Any]) -> None:
        if table == "activity" and op == "update":
            raise RuntimeError("db unavailable")
        original_write(table, op, row_id, payload)

    monkeypatch.setattr(store, "write", write)
    try:
        assert _announce(store, _row(), home=tmp_path) is None
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "pending"


def test_announce_status_reports_the_comment_was_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    try:
        assert _announce(store, _row(), home=tmp_path) is True
    finally:
        store.close()


def test_announce_status_reports_a_safely_retired_announcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The executor left the row pending; the retirement write makes it safe,
    # so dispatch may start the job.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = runner_status.status_activity_id("announce", "job-1", 0)

    def unfinished_execute(_store: Store, _runner: Any, _activity_id: str) -> str:
        return "comment.post pending"

    try:
        assert _announce(store, _row(), home=tmp_path, execute=unfinished_execute) is False
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "error"
    assert saved["error"] == "announcement not posted before the job started"


def test_announce_status_reports_unsafe_when_the_retirement_write_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The row is still pending and the retirement could not write, so a later
    # scan may post it after the baseline. Dispatch must not start the job.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    original_write = store.write

    def write(table: str, op: str, row_id: str, payload: dict[str, Any]) -> None:
        # Queueing inserts the activity; only the retirement update fails.
        if table == "activity" and op == "update":
            raise RuntimeError("db unavailable")
        original_write(table, op, row_id, payload)

    def unfinished_execute(_store: Store, _runner: Any, _activity_id: str) -> str:
        return "comment.post pending"

    monkeypatch.setattr(store, "write", write)
    try:
        assert _announce(store, _row(), home=tmp_path, execute=unfinished_execute) is None
    finally:
        store.close()


def test_post_status_does_not_retire_an_outcome_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An outcome comment is retried on the next pass; the executor's marker
    # keeps the retry at one comment. Retiring it would stop that retry.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = runner_status.status_activity_id("outcome", "job-1", 0)

    def raising_execute(*_args: Any) -> str:
        raise RuntimeError("gh unavailable")

    try:
        assert (
            _post(
                store,
                _row(),
                home=tmp_path,
                kind="outcome",
                execute=raising_execute,
            )
            is False
        )
        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "pending"
    assert "error" not in saved


def test_post_status_adopts_an_already_done_activity_without_executing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A re-run of the same attempt finds the row the first run completed; the
    # deterministic id is what keeps the comment single.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    activity_id = runner_status.status_activity_id("announce", "job-1", 0)
    executed: list[str] = []

    def execute(*_args: Any) -> str:
        executed.append("called")
        raise AssertionError("a done activity must not be executed")

    try:
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "s",
                "type": "comment.post",
                "execution_status": "done",
                "payload": {"body": "previous"},
            },
        )

        assert _post(store, _row(), home=tmp_path, execute=execute) is True
    finally:
        store.close()

    assert executed == []


# -------------------------------------------------- retire_status_activities


def _status_activity_id(kind: str, *, attempts: int = 0) -> str:
    return runner_status.status_activity_id(kind, "job-1", attempts)


def _activity_payload(activity_id: str, status: str) -> dict[str, Any]:
    return {
        "id": activity_id,
        "session_id": "s",
        "type": "comment.post",
        "execution_status": status,
        "payload": {"body": "previous"},
    }


def test_retire_status_activities_retires_both_pending_activities(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    try:
        ids = [_status_activity_id(kind) for kind in ("announce", "outcome")]
        for activity_id in ids:
            store.write(
                "activity", "insert", activity_id, _activity_payload(activity_id, "pending")
            )

        assert runner_status.retire_status_activities(
            store, {"id": "job-1", "attempts": 0}
        ) is True

        for activity_id in ids:
            saved = store.row("activity", activity_id)
            assert saved is not None
            assert saved["execution_status"] == "error"
            assert saved["error"] == "superseded by a retry of the job"
    finally:
        store.close()


def test_retire_status_activities_leaves_a_done_activity_alone(tmp_path: Path) -> None:
    store = Store(tmp_path)
    activity_id = _status_activity_id("announce")
    try:
        store.write(
            "activity", "insert", activity_id, _activity_payload(activity_id, "done")
        )

        # The outcome activity is absent; both rows are fine.
        assert runner_status.retire_status_activities(
            store, {"id": "job-1", "attempts": 0}
        ) is True

        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "done"
    assert "error" not in saved


def test_retire_status_activities_accepts_absent_activities(tmp_path: Path) -> None:
    store = Store(tmp_path)
    try:
        assert runner_status.retire_status_activities(
            store, {"id": "job-1", "attempts": 3}
        ) is True
        assert store.rows("activity") == []
    finally:
        store.close()


def test_retire_status_activities_refuses_a_foreign_pending_activity(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    activity_id = _status_activity_id("outcome")
    try:
        store.apply_remote(
            {
                "origin_device_id": "other-device",
                "origin_seq": 1,
                "table": "activity",
                "op": "insert",
                "row_id": activity_id,
                "payload": _activity_payload(activity_id, "pending"),
                "occurred_at": "2026-08-13T12:00:00Z",
            }
        )

        assert runner_status.retire_status_activities(
            store, {"id": "job-1", "attempts": 0}
        ) is False

        saved = store.row("activity", activity_id)
    finally:
        store.close()

    assert saved is not None
    assert saved["execution_status"] == "pending"


def test_retire_status_activities_reports_a_raising_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    activity_id = _status_activity_id("announce")

    def write(table: str, op: str, row_id: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("db unavailable")

    try:
        store.write(
            "activity", "insert", activity_id, _activity_payload(activity_id, "pending")
        )
        monkeypatch.setattr(store, "write", write)

        assert runner_status.retire_status_activities(
            store, {"id": "job-1", "attempts": 0}
        ) is False
    finally:
        store.close()


# ------------------------------------------------------------ report_outcomes


def _failed_row(ref: str = "7", **overrides: Any) -> dict[str, Any]:
    row = job_row(
        session_id="s",
        repo="owner/name",
        ref=ref,
        job_type="pr-review",
        actor="alice",
    )
    row.update(
        {
            "state": "failed",
            "outcome": "agent_failed",
            "reported": False,
            "pull_request": False,
        }
    )
    row.update(overrides)
    return row


def _report(
    store: Store,
    *,
    home: Path,
    expected_login: str = "example-bot",
    execute: Any = None,
) -> tuple[list[str], list[str]]:
    return runner_status.report_outcomes(
        store,
        store,
        _runner,
        home=home,
        expected_login=expected_login,
        execute_activity=execute or _done_executor(),
    )


def test_report_outcomes_posts_the_comment_and_marks_the_row_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    row = _failed_row(exit_code=1)
    seen: list[dict[str, Any]] = []
    try:
        store.write("job", "insert", row["id"], row)
        reported, failed = _report(store, home=tmp_path, execute=_done_executor(seen))
        saved = store.row("job", row["id"])
    finally:
        store.close()

    assert reported == [row["id"]]
    assert failed == []
    assert saved is not None
    assert saved["reported"] is True
    assert "report_attempts" not in saved
    assert seen[0]["payload"]["target"] == "issue"
    assert "exit code 1" in seen[0]["payload"]["body"]


def test_report_outcomes_only_reports_rows_marked_reported_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row written before this change has no key at all, and a delivered
    # report must not be repeated.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    old = _failed_row("7")
    old.pop("reported")
    already = _failed_row("8", reported=True)
    owed = _failed_row("9")
    try:
        for row in (old, already, owed):
            store.write("job", "insert", row["id"], row)

        reported, failed = _report(store, home=tmp_path)
        saved_old = store.row("job", old["id"])
        saved_already = store.row("job", already["id"])
        saved_owed = store.row("job", owed["id"])
    finally:
        store.close()

    assert reported == [owed["id"]]
    assert failed == []
    assert saved_old is not None
    assert "reported" not in saved_old
    assert saved_already is not None
    assert saved_already["reported"] is True
    assert saved_owed is not None
    assert saved_owed["reported"] is True


@pytest.mark.parametrize("outcome", ["success", "mystery"])
def test_report_outcomes_skips_an_outcome_without_a_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    row = _failed_row(outcome=outcome)
    try:
        store.write("job", "insert", row["id"], row)

        reported, failed = _report(store, home=tmp_path)
        saved = store.row("job", row["id"])
    finally:
        store.close()

    assert reported == []
    assert failed == []
    assert saved is not None
    assert saved["reported"] is False


def test_report_outcomes_skips_a_malformed_outcome_and_reports_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unhashable outcome (`[]`) would raise out of the membership test and
    # abort the whole pass, leaving every other row unreported.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    broken = _failed_row("7", outcome=[])
    good = _failed_row("8")
    try:
        store.write("job", "insert", broken["id"], broken)
        store.write("job", "insert", good["id"], good)

        reported, failed = _report(store, home=tmp_path)
        saved_broken = store.row("job", broken["id"])
        saved_good = store.row("job", good["id"])
    finally:
        store.close()

    assert reported == [good["id"]]
    assert failed == []
    assert saved_broken is not None
    assert saved_broken["reported"] is False
    assert "report_attempts" not in saved_broken
    assert saved_good is not None
    assert saved_good["reported"] is True


def test_report_outcomes_retries_a_failed_post_and_continues_with_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    # The target of a post comes from `pull_request`; without it the row
    # cannot name the right issue or pull request and the post fails closed.
    broken = _failed_row("7")
    broken.pop("pull_request")
    good = _failed_row("8")
    try:
        store.write("job", "insert", broken["id"], broken)
        store.write("job", "insert", good["id"], good)

        reported, failed = _report(store, home=tmp_path)
        saved_broken = store.row("job", broken["id"])
        saved_good = store.row("job", good["id"])
    finally:
        store.close()

    assert reported == [good["id"]]
    assert failed == [broken["id"]]
    assert saved_broken is not None
    assert saved_broken["reported"] is False
    assert saved_broken["report_attempts"] == 1
    assert saved_good is not None
    assert saved_good["reported"] is True


def test_report_outcomes_stops_at_the_report_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    row = _failed_row(
        "7", report_attempts=runner_status.MAX_REPORT_ATTEMPTS
    )
    executed: list[str] = []

    def execute(*_args: Any) -> str:
        executed.append("called")
        raise AssertionError("an exhausted row must not be posted")

    try:
        store.write("job", "insert", row["id"], row)

        reported, failed = _report(store, home=tmp_path, execute=execute)
        saved = store.row("job", row["id"])
    finally:
        store.close()

    assert reported == []
    assert failed == []
    assert executed == []
    assert saved is not None
    assert saved["report_attempts"] == runner_status.MAX_REPORT_ATTEMPTS
    assert saved["reported"] is False


@pytest.mark.parametrize(
    ("outcome", "minutes"),
    [("timeout", 12), ("stalled", 7)],
)
def test_report_outcomes_uses_the_limit_the_watchdog_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    minutes: int,
) -> None:
    # The row's `limit_minutes` is what the watchdog used. Reading the current
    # runner configuration would report a limit the job never had.
    _install_account(monkeypatch)
    store = Store(tmp_path)
    row = _failed_row("7", outcome=outcome, limit_minutes=minutes)
    seen: list[dict[str, Any]] = []
    try:
        store.write("job", "insert", row["id"], row)

        reported, failed = _report(
            store,
            home=tmp_path,
            execute=_done_executor(seen),
        )
    finally:
        store.close()

    assert reported == [row["id"]]
    assert failed == []
    assert f"{minutes} minutes" in seen[0]["payload"]["body"]


def test_report_outcomes_omits_the_minutes_without_a_recorded_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    row = _failed_row("7", outcome="timeout")
    seen: list[dict[str, Any]] = []
    try:
        store.write("job", "insert", row["id"], row)

        reported, failed = _report(
            store,
            home=tmp_path,
            execute=_done_executor(seen),
        )
    finally:
        store.close()

    assert reported == [row["id"]]
    assert failed == []
    assert "exceeded its time limit and was stopped" in seen[0]["payload"]["body"]


def test_report_outcomes_counts_a_failed_write_and_leaves_the_row_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    row = _failed_row(exit_code=1)
    store.write("job", "insert", row["id"], row)
    original_write = store.write

    def write(table: str, op: str, row_id: str, payload: dict[str, Any]) -> None:
        # The activity write still succeeds; only the job marker fails.
        if table == "job":
            raise RuntimeError("db unavailable")
        original_write(table, op, row_id, payload)

    monkeypatch.setattr(store, "write", write)
    try:
        reported, failed = _report(store, home=tmp_path)
        saved = store.row("job", row["id"])
    finally:
        store.close()

    assert reported == []
    assert failed == [row["id"]]
    assert saved is not None
    assert saved["reported"] is False
    assert "report_attempts" not in saved


def test_report_outcomes_continues_after_one_row_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_account(monkeypatch)
    store = Store(tmp_path)
    first = _failed_row("7")
    second = _failed_row("8")

    def post(
        _store: Store, _runner: Any, *, row: dict[str, Any], **_kwargs: Any
    ) -> bool:
        if row["id"] == first["id"]:
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(runner_status, "post_status", post)
    try:
        store.write("job", "insert", first["id"], first)
        store.write("job", "insert", second["id"], second)

        reported, failed = _report(store, home=tmp_path)
        saved_first = store.row("job", first["id"])
        saved_second = store.row("job", second["id"])
    finally:
        store.close()

    assert reported == [second["id"]]
    assert failed == [first["id"]]
    assert saved_first is not None
    assert saved_first["reported"] is False
    assert saved_second is not None
    assert saved_second["reported"] is True
