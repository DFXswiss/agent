"""Announce a job and report how it ended, through the activity store.

The person who mentioned the bot sees one status comment per job. The first
status activity creates it; every later one edits the same comment in place. A
failure overwrites the announcement with the failure text, and a retry's
announcement overwrites the previous outcome. A successful job has no outcome
text, because its posted review is the output, so its comment keeps the
announcement.

Every status activity is an ordinary `comment.post` row. They are inserted with
a deterministic id, executed through the script-owned GitHub executor, and
never posted by a direct `gh` call from the runner. All activities of one job
generation carry the same `edit_marker`: the first one posts the comment and
the executor edits the newest own comment that carries it. The activity id
still distinguishes the attempt, so re-running one attempt adopts its own row.

The announcement belongs before the output baseline in dispatch. The ordering
still matters even though later activities edit: the first announcement of a
job is what creates the comment, and an edit never creates a new comment id, so
a later status edit can never count as new output. An announcement that
appears after the baseline would be a new comment and make the silent-failure
check pass every job. This module only posts; the ordering is enforced by
`dispatch.dispatch_queued` and documented there.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import github_accounts, workspace
from .github_accounts import AccountError as GitHubAccountError
from .runtime import Completed
from .store import Store, StoreError, utcnow

# The outcomes that get an outcome comment. `success` is deliberately absent:
# a successful job's posted review is its output, so a second comment would
# only repeat it.
REPORTED_OUTCOMES = frozenset(
    {"agent_failed", "timeout", "stalled", "crashed", "workspace_failed"}
)

# A comment that cannot be posted is retried by the next supervise pass. The
# bound keeps a row that can never post (a deleted repository, a revoked
# account) from retrying forever.
MAX_REPORT_ATTEMPTS = 5

_KINDS = ("announce", "outcome")

# The exact marker `status_marker` produces, and the only `edit_marker` the
# GitHub executor accepts. Keeping one compiled pattern for the producer and
# the validator means a lane cannot pass another module's HTML comment as an
# `edit_marker` and overwrite that comment. The job id uses the safe alphabet,
# without the `--` an HTML comment cannot contain; the generation is a UTC
# timestamp.
STATUS_MARKER_RE = re.compile(
    r"<!-- agent-runner-job:(?![^:]*--)[A-Za-z0-9_-]+:"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z -->"
)


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _comment(english: str, german: str) -> str:
    """One English paragraph followed by the collapsed German translation.

    The blank lines inside the `<details>` element are what make GitHub render
    the German text as a paragraph instead of raw Markdown.
    """
    return (
        f"{english}\n\n"
        "<details><summary>DE</summary>\n\n"
        f"{german}\n\n"
        "</details>"
    )


def _attempt_suffix(attempts: Any, english: bool) -> str:
    """` (attempt N)` / ` (Versuch N)` for a retry, else an empty string."""
    if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 1:
        label = "attempt" if english else "Versuch"
        return f" ({label} {attempts + 1})"
    return ""


def status_marker(job_id: str, created_at: str) -> str:
    """The HTML marker that ties one job generation's status comment together.

    Every status activity of a generation carries this marker as the
    `comment.post` payload's `edit_marker`. The first activity creates the
    comment; a later one edits the newest own comment that carries it, so the
    job keeps one status comment instead of one per activity.

    Both parts are interpolated into an HTML comment, so the result must match
    `STATUS_MARKER_RE`; a job id outside the safe alphabet or a generation that
    is not a UTC timestamp is refused rather than producing a marker the
    executor would reject later.
    """
    if not isinstance(job_id, str) or not isinstance(created_at, str):
        raise ValueError("job id and generation (created_at) must be strings")
    marker = f"<!-- agent-runner-job:{job_id}:{created_at} -->"
    if STATUS_MARKER_RE.fullmatch(marker) is None:
        raise ValueError(
            "job id must be [A-Za-z0-9_-]+ without '--' and generation "
            "(created_at) must be a UTC timestamp like 2026-01-01T00:00:00Z"
        )
    return marker


def announce_body(job_id: str, attempts: int, *, marker: str) -> str:
    """The comment posted when dispatch starts a job."""
    english = (
        f"Working on this now: job `{job_id}`"
        f"{_attempt_suffix(attempts, True)}."
    )
    german = (
        f"Ich arbeite jetzt daran: Auftrag `{job_id}`"
        f"{_attempt_suffix(attempts, False)}."
    )
    return f"{_comment(english, german)}\n{marker}"


def outcome_body(
    job_id: str,
    outcome: str,
    *,
    exit_code: int | None,
    minutes: int | None,
    marker: str,
) -> str | None:
    """The comment describing how a failed job ended, or None.

    A job that ended successfully has no outcome comment: the review it posted
    is the result. `judge` can downgrade a zero exit code to `agent_failed`
    when nothing was published; that case has no useful exit code and uses the
    generic sentence.
    """
    if outcome == "agent_failed":
        if (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        ):
            english = f"Job `{job_id}` ended with exit code {exit_code}."
            german = f"Auftrag `{job_id}` endete mit Fehlercode {exit_code}."
        else:
            english = f"Job `{job_id}` ended without publishing a result."
            german = f"Auftrag `{job_id}` endete, ohne ein Ergebnis zu veröffentlichen."
    elif outcome == "timeout":
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            english = (
                f"Job `{job_id}` exceeded its time limit of {minutes} minutes."
            )
            german = (
                f"Auftrag `{job_id}` hat das Zeitlimit von {minutes} Minuten "
                "überschritten."
            )
        else:
            english = f"Job `{job_id}` exceeded its time limit."
            german = f"Auftrag `{job_id}` hat das Zeitlimit überschritten."
    elif outcome == "stalled":
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            english = (
                f"Job `{job_id}` showed no progress for {minutes} minutes."
            )
            german = (
                f"Auftrag `{job_id}` hat {minutes} Minuten keinen Fortschritt "
                "gezeigt."
            )
        else:
            english = f"Job `{job_id}` stopped making progress."
            german = f"Auftrag `{job_id}` hat keinen Fortschritt mehr gezeigt."
    elif outcome == "crashed":
        english = f"Job `{job_id}` stopped unexpectedly."
        german = f"Auftrag `{job_id}` ist unerwartet abgebrochen."
    elif outcome == "workspace_failed":
        english = f"Job `{job_id}`: the working copy could not be prepared."
        german = f"Auftrag `{job_id}`: Die Arbeitskopie konnte nicht hergestellt werden."
    else:
        return None
    return f"{_comment(english, german)}\n{marker}"


def status_activity_id(
    kind: str, job_id: str, created_at: str, attempts: int
) -> str:
    """Deterministic activity id for one status comment of one job generation.

    The generation is the row's `created_at`. `prune` deletes a finished row
    but keeps its activities, and a later mention of the same pull request
    inserts a new row with the same id and `attempts: 0`. Without the
    generation the new job would adopt the earlier one's `done` activities and
    post no announcement or outcome comment of its own.

    A retry keeps the row's `created_at`, so it stays in its generation and a
    new `attempts` still gives it new ids. Re-running the same attempt finds
    the row it already wrote, and the executor's marker keeps the comment
    single.
    """
    if kind not in _KINDS:
        raise ValueError("status kind must be announce or outcome")
    if not isinstance(created_at, str) or not created_at:
        raise ValueError("job generation (created_at) must be a non-empty string")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"runner-status:{kind}:{job_id}:{created_at}:{attempts}",
        )
    )


def queue_activity(
    store: Store,
    *,
    activity_id: str,
    session_id: str,
    activity_type: str,
    payload: dict[str, Any],
    account: github_accounts.Account,
) -> bool:
    """Insert or re-open one deterministic activity.

    The account validated by the caller is pinned on a new row in the same
    shape `github_act` uses, so the executor refuses a session binding that
    changes before execution. A row from another device is an error: this
    device does not post through a foreign row.

    Returns True when the activity still has to be executed. A row that is
    already `done` is left exactly as it is and needs no second posting; a row
    that failed is reset to `pending` so this attempt executes it again. The
    stored `execution_account` is kept, so a retry cannot switch the account
    that posts the comment.
    """
    row: dict[str, Any] = {
        "id": activity_id,
        "session_id": session_id,
        "type": activity_type,
        "payload": payload,
        "execution_status": "pending",
    }
    existing = store.row("activity", activity_id)
    if existing is None:
        row["execution_account"] = {
            "account": account.name,
            "login": account.login.casefold(),
        }
        store.write("activity", "insert", activity_id, row)
        return True
    if existing.get("_origin_device_id") != store.device_id():
        raise ValueError("activity belongs to another device")
    if existing.get("execution_status") == "done":
        return False
    if existing.get("execution_account") is not None:
        row["execution_account"] = existing["execution_account"]
    store.write("activity", "update", activity_id, row)
    return True


def _ref_number(ref: Any) -> int | None:
    """The positive number carried by `#12` or `12`, or None."""
    if isinstance(ref, str):
        text = ref[1:] if ref.startswith("#") else ref
        if text and text.isascii() and text.isdigit():
            value = int(text)
            if value > 0:
                return value
    return None


def _retire_activity(
    store: Store, activity_id: str, *, error: str
) -> bool | None:
    """Retire one pending status activity; report whether it may still run.

    Under the executor's per-activity lock, a still-pending row owned by this
    device is rewritten to `execution_status: "error"`, which a later scan
    does not execute. A row that is absent, already `done`, or no longer
    `pending` needs no write and cannot be executed again.

    Returns True when the row is `done` (the comment was posted), False when
    it is safe but was not posted (absent, not pending, or retired here), and
    None when it may still be executed later: a pending row owned by another
    device, or a retirement write that raised.
    """
    try:
        # Imported here rather than at module top: `github_act` is the
        # executor this module is called from, and a lazy import keeps the
        # dependency one way even if that module ever grows a `runner_status`
        # import. A failed import is still only a failed retirement.
        from . import github_act

        with store.exclusive(github_act.github_activity_lock(activity_id)):
            current = store.row("activity", activity_id)
            if not isinstance(current, dict):
                return False
            if current.get("execution_status") == "done":
                return True
            if current.get("execution_status") != "pending":
                return False
            if current.get("_origin_device_id") != store.device_id():
                return None
            updated = _strip(current)
            updated["execution_status"] = "error"
            # The reason belongs on `execution_error`, the key every GitHub
            # activity path writes and reads: `github_act._mark` stores it
            # through `_safe_execution_error`, which redacts account paths and
            # bounds the length. Because the whole row is written back, an
            # older failed execution's reason is replaced rather than shown.
            updated["execution_error"] = github_act._safe_execution_error(
                store, current, error
            )
            store.write("activity", "update", activity_id, updated)
            return False
    except (Exception, SystemExit):
        return None


def _retire_unposted_announcement(
    store: Store, activity_id: str
) -> bool | None:
    """Make sure a failed announcement can never be posted later.

    `post_status` returns False when the executor raises or leaves the row
    pending, but the activity itself can stay `pending`. A scheduled
    `scan_github` executes every pending GitHub activity of this device, so it
    would post the announcement after dispatch took the output baseline, and
    `finalize` would count the runner's own comment as the job's output.

    Returns True when the row ended `done`, False when it is safe but was not
    posted, and None when it may still be posted later.
    """
    return _retire_activity(
        store,
        activity_id,
        error="announcement not posted before the job started",
    )


def retire_status_activities(
    store: Store,
    row: dict[str, Any],
    *,
    error: str = "superseded by a retry of the job",
) -> bool:
    """Retire a superseded job's pending status activities.

    A retry writes a new attempt's row, and `prune` deletes a finished row; in
    both cases an announcement or outcome activity of the row's current
    attempt can still be `pending` after an interrupted dispatch or supervise.
    A later scan would post it after the next attempt's baseline, and
    `finalize` would count it as that attempt's output. Under each activity's
    own lock, such a row is retired exactly as an unposted announcement is. A
    row that is absent, done or no longer pending is fine and left alone, as
    is a pending row this device retires.

    `error` is the text recorded on a retired row, so prune and retry each say
    what superseded the comment. The row's `created_at` names the generation,
    so a re-ingested job with the same id never retires an earlier
    generation's activities.

    Returns True only when both activities are fine; a pending row owned by
    another device, a raising write, or any other exception returns False.
    """
    try:
        job_id = row.get("id")
        created_at = row.get("created_at")
        attempts = row.get("attempts")
        if (
            not isinstance(job_id, str)
            or not isinstance(created_at, str)
            or not created_at
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
        ):
            return False
        for kind in _KINDS:
            activity_id = status_activity_id(kind, job_id, created_at, attempts)
            if _retire_activity(store, activity_id, error=error) is None:
                return False
        return True
    except (Exception, SystemExit):
        return False


def _post_status(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    row: dict[str, Any],
    kind: str,
    body: str,
    home: Path,
    expected_login: str,
    execute_activity: Callable[
        [Store, Callable[[list[str]], Completed], str], str | None
    ],
) -> bool | None:
    """Post one status comment for `row`.

    An outcome returns True only when it ended `done` and False otherwise; a
    failed outcome is retried by the next pass. An announcement also has the
    third result None: the comment was not posted but its activity may still
    be executed by a later scan.

    Every input that could name the wrong target or post under the wrong
    account fails closed with False rather than raising.
    """
    try:
        if not isinstance(row, dict):
            return False
        session_id = row.get("session_id")
        if not isinstance(session_id, str):
            return False
        repo = row.get("repo")
        try:
            workspace.repo_slug(repo)
        except ValueError:
            return False
        number = _ref_number(row.get("ref"))
        if number is None:
            return False
        pull_request = row.get("pull_request")
        if not isinstance(pull_request, bool):
            return False
        attempts = row.get("attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            return False
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id:
            return False
        created_at = row.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            return False
        if not isinstance(expected_login, str):
            return False
        try:
            account = github_accounts.load_accounts(home).for_session(session_id)
        except (GitHubAccountError, StoreError):
            return False
        if account.login.casefold() != expected_login.casefold():
            return False
        try:
            activity_id = status_activity_id(kind, job_id, created_at, attempts)
            marker = status_marker(job_id, created_at)
        except ValueError:
            return False
        payload = {
            "repo": repo,
            "number": number,
            "body": body,
            "target": "pr" if pull_request else "issue",
            "edit_marker": marker,
        }
        try:
            needs_execution = queue_activity(
                store,
                activity_id=activity_id,
                session_id=session_id,
                activity_type="comment.post",
                payload=payload,
                account=account,
            )
        except (Exception, SystemExit):
            return False
        if not needs_execution:
            # A row from an earlier pass already completed this exact attempt.
            return True
        try:
            execute_activity(store, runner, activity_id)
        except (Exception, SystemExit):
            if kind == "announce":
                return _retire_unposted_announcement(store, activity_id)
            return False
        executed = store.row("activity", activity_id)
        if (
            isinstance(executed, dict)
            and executed.get("execution_status") == "done"
        ):
            return True
        if kind == "announce":
            # The executor did not reach `done`, but the row may still be
            # pending and would be posted by a later scan.
            return _retire_unposted_announcement(store, activity_id)
        return False
    except (Exception, SystemExit):
        # An announcement that failed unexpectedly may have left a pending
        # row behind, so it is unsafe rather than safely unposted.
        return None if kind == "announce" else False


def announce_status(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    row: dict[str, Any],
    body: str,
    home: Path,
    expected_login: str,
    execute_activity: Callable[
        [Store, Callable[[list[str]], Completed], str], str | None
    ],
) -> bool | None:
    """Post one announcement for `row`, with the third result.

    Returns True when the comment ended `done`, False when it was not posted
    and can no longer be posted, and None when its activity may still be
    executed later. Dispatch only starts a job on True or False: an
    announcement that can still appear after the output baseline would be
    counted by `finalize` as the job's own output.

    Every result short of a posted comment ends with the locked check on this
    attempt's activity row, because an earlier interrupted dispatch can have
    left that row `pending`. `_post_status` returns False on paths that never
    look at the row (a rejected input, a raising queue write); treating that
    as safe would let a later scan post the old announcement after the
    baseline.
    """
    posted = _post_status(
        store,
        runner,
        row=row,
        kind="announce",
        body=body,
        home=home,
        expected_login=expected_login,
        execute_activity=execute_activity,
    )
    if posted is True:
        return True
    if not isinstance(row, dict):
        return False
    job_id = row.get("id")
    created_at = row.get("created_at")
    attempts = row.get("attempts")
    if (
        not isinstance(job_id, str)
        or not job_id
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(attempts, int)
        or isinstance(attempts, bool)
    ):
        # No non-empty id, generation or valid attempt means no activity row
        # of this attempt can exist.
        return False
    try:
        activity_id = status_activity_id("announce", job_id, created_at, attempts)
    except (Exception, SystemExit):
        return None
    return _retire_unposted_announcement(store, activity_id)


def post_status(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    row: dict[str, Any],
    kind: str,
    body: str,
    home: Path,
    expected_login: str,
    execute_activity: Callable[
        [Store, Callable[[list[str]], Completed], str], str | None
    ],
) -> bool:
    """Post one status comment for `row`; True only when it ended `done`.

    This bool result is what an outcome needs: a failed comment is retried by
    the next pass. An announcement must go through `announce_status`, whose
    third result says the comment may still be posted later; collapsing that
    to False here would let dispatch start a job whose comment can still land
    after the baseline.
    """
    return _post_status(
        store,
        runner,
        row=row,
        kind=kind,
        body=body,
        home=home,
        expected_login=expected_login,
        execute_activity=execute_activity,
    ) is True


def report_outcomes(
    jobs: Any,
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    home: Path,
    expected_login: str,
    execute_activity: Callable[
        [Store, Callable[[list[str]], Completed], str], str | None
    ],
) -> tuple[list[str], list[str]]:
    """Post the outcome comment for every failed job that still needs one.

    `jobs` supplies and receives the job rows; the runner passes its view of
    this device's jobs. `store` is the real store: the activity row and the
    executor's per-row lock, account binding and home live there, and a
    restricted job view provides none of them.

    The minutes in the text come from the row's `limit_minutes`, which
    finalize recorded when it applied the budget. Reading the current
    configuration instead would report a limit the watchdog never used after
    the setting changed.

    Returns (reported ids, failed ids). Only a row whose `reported` is exactly
    False is considered: a row written before this change has no such key and
    is never reported, and a row already reported stays untouched. A failed
    post increments `report_attempts` and returns the id as failed, so the next
    pass tries again, up to `MAX_REPORT_ATTEMPTS`; after that the row is left
    alone.

    One bad row never stops the others. A write that fails leaves the row as it
    was and counts the id as failed, so the next pass retries it.
    """
    reported: list[str] = []
    failed: list[str] = []

    for row in jobs.rows("job"):
        if not isinstance(row, dict):
            continue
        if row.get("state") != "failed":
            continue
        # Missing means "written before this change": never report it.
        if row.get("reported") is not False:
            continue
        outcome = row.get("outcome")
        # A non-string (a list or object in a malformed row) is unhashable and
        # would raise out of the membership test, aborting the whole pass.
        if not isinstance(outcome, str) or outcome not in REPORTED_OUTCOMES:
            continue
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        attempts = row.get("report_attempts", 0)
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            continue
        if attempts >= MAX_REPORT_ATTEMPTS:
            continue

        try:
            marker = status_marker(job_id, row.get("created_at"))
        except ValueError:
            # Without a marker this generation has no comment to edit, so
            # `post_status` would refuse it too. Count it as a failed report,
            # like the other unusable inputs, so the attempt bound still stops
            # a row that can never carry a marker.
            updated = _strip(row)
            updated["report_attempts"] = attempts + 1
            try:
                jobs.write("job", "update", job_id, updated)
            except (Exception, SystemExit):
                pass
            failed.append(job_id)
            continue
        try:
            exit_code: int | None = None
            raw_exit = row.get("exit_code")
            if isinstance(raw_exit, int) and not isinstance(raw_exit, bool):
                exit_code = raw_exit
            minutes: int | None = None
            raw_minutes = row.get("limit_minutes")
            if isinstance(raw_minutes, int) and not isinstance(raw_minutes, bool):
                minutes = raw_minutes
            body = outcome_body(
                job_id,
                outcome,
                exit_code=exit_code,
                minutes=minutes,
                marker=marker,
            )
            if body is None:
                continue
            posted = post_status(
                store,
                runner,
                row=row,
                kind="outcome",
                body=body,
                home=home,
                expected_login=expected_login,
                execute_activity=execute_activity,
            )
        except (Exception, SystemExit):
            failed.append(job_id)
            continue

        updated = _strip(row)
        if posted:
            updated["reported"] = True
            updated["updated_at"] = utcnow()
        else:
            updated["report_attempts"] = attempts + 1
        try:
            jobs.write("job", "update", job_id, updated)
        except (Exception, SystemExit):
            # The row is unchanged (the write is transactional), so the next
            # pass tries this id again.
            failed.append(job_id)
            continue
        if posted:
            reported.append(job_id)
        else:
            # The row is retried on the next pass; `supervise` reports the id
            # so an operator sees that the comment is still owed.
            failed.append(job_id)

    return reported, failed
