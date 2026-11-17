"""Announce a job and report how it ended, through the activity store.

The person who mentioned the bot sees two comments: one when dispatch starts
the job, and one when a job ends as failed. A successful job gets no outcome
comment, because its posted review is the output.

Both comments are ordinary `comment.post` activities. They are inserted with a
deterministic id, executed through the script-owned GitHub executor, and never
posted by a direct `gh` call from the runner. The executor's marker makes a
re-run of the same attempt adopt its own comment instead of posting a second
one, so the id alone is enough to keep one comment per attempt.

The announcement belongs before the output baseline in dispatch, so the
baseline already holds it. An announcement that appears after the baseline
would count as the job's new output and make the silent-failure check pass
every job. This module only
posts; the ordering is enforced by `dispatch.dispatch_queued` and documented
there.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import finalize, github_accounts, workspace
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


def announce_body(job_id: str, attempts: int) -> str:
    """The comment posted when dispatch starts a job."""
    english = (
        f"Working on this now: job `{job_id}`"
        f"{_attempt_suffix(attempts, True)}."
    )
    german = (
        f"Ich arbeite jetzt daran: Auftrag `{job_id}`"
        f"{_attempt_suffix(attempts, False)}."
    )
    return _comment(english, german)


def outcome_body(
    job_id: str,
    outcome: str,
    *,
    exit_code: int | None,
    minutes: int | None,
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
            english = (
                f"Job `{job_id}` ended with exit code {exit_code}; "
                "nothing was published."
            )
            german = (
                f"Auftrag `{job_id}` endete mit Fehlercode {exit_code}; "
                "es wurde nichts veröffentlicht."
            )
        else:
            english = f"Job `{job_id}` ended without publishing a result."
            german = f"Auftrag `{job_id}` endete, ohne ein Ergebnis zu veröffentlichen."
    elif outcome == "timeout":
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            english = (
                f"Job `{job_id}` exceeded its time limit of {minutes} minutes "
                "and was stopped."
            )
            german = (
                f"Auftrag `{job_id}` hat das Zeitlimit von {minutes} Minuten "
                "überschritten und wurde beendet."
            )
        else:
            english = f"Job `{job_id}` exceeded its time limit and was stopped."
            german = (
                f"Auftrag `{job_id}` hat das Zeitlimit überschritten und wurde "
                "beendet."
            )
    elif outcome == "stalled":
        if isinstance(minutes, int) and not isinstance(minutes, bool):
            english = (
                f"Job `{job_id}` showed no progress for {minutes} minutes "
                "and was stopped."
            )
            german = (
                f"Auftrag `{job_id}` hat {minutes} Minuten keinen Fortschritt "
                "gezeigt und wurde beendet."
            )
        else:
            english = f"Job `{job_id}` stopped making progress and was stopped."
            german = (
                f"Auftrag `{job_id}` hat keinen Fortschritt mehr gezeigt und "
                "wurde beendet."
            )
    elif outcome == "crashed":
        english = f"Job `{job_id}` stopped unexpectedly."
        german = f"Auftrag `{job_id}` ist unerwartet abgebrochen."
    elif outcome == "workspace_failed":
        english = f"Job `{job_id}`: the working copy could not be prepared."
        german = f"Auftrag `{job_id}`: Die Arbeitskopie konnte nicht hergestellt werden."
    else:
        return None
    return _comment(english, german)


def status_activity_id(kind: str, job_id: str, attempts: int) -> str:
    """Deterministic activity id for one status comment of one attempt.

    A retry has a new `attempts`, so it derives new ids and gets its own
    comments. Re-running the same attempt finds the row it already wrote, and
    the executor's marker keeps the comment single.
    """
    if kind not in _KINDS:
        raise ValueError("status kind must be announce or outcome")
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"runner-status:{kind}:{job_id}:{attempts}")
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


def _retire_unposted_announcement(store: Store, activity_id: str) -> bool:
    """Make sure a failed announcement can never be posted later.

    `post_status` returns False when the executor raises or leaves the row
    pending, but the activity itself can stay `pending`. A scheduled
    `scan_github` executes every pending GitHub activity of this device, so it
    would post the announcement after dispatch took the output baseline, and
    `finalize` would count the runner's own comment as the job's output. Under
    the executor's per-activity lock, a still-pending row owned by this device
    is retired to `error`, which a later scan does not execute. A row that is
    already `done` posted its comment before the baseline and is left alone.

    Outcome activities are deliberately not retired: a later pass re-opens and
    retries them, and the executor's marker keeps a retry to one comment.

    Returns True only when the row ended `done`; any failure returns False.
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
            if (
                current.get("execution_status") != "pending"
                or current.get("_origin_device_id") != store.device_id()
            ):
                return False
            updated = _strip(current)
            updated["execution_status"] = "error"
            updated["error"] = "announcement not posted before the job started"
            store.write("activity", "update", activity_id, updated)
            return False
    except (Exception, SystemExit):
        return False


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

    Every input that could name the wrong target or post under the wrong
    account fails closed with False rather than raising. A failed comment is
    retried by the next pass (for an outcome) or simply does not stop the job
    (for an announcement).
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
        if not isinstance(expected_login, str):
            return False
        try:
            account = github_accounts.load_accounts(home).for_session(session_id)
        except (GitHubAccountError, StoreError):
            return False
        if account.login.casefold() != expected_login.casefold():
            return False
        try:
            activity_id = status_activity_id(kind, job_id, attempts)
        except ValueError:
            return False
        payload = {
            "repo": repo,
            "number": number,
            "body": body,
            "target": "pr" if pull_request else "issue",
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
        return False


def report_outcomes(
    jobs: Any,
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    runner_config: Any,
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
            exit_code: int | None = None
            raw_exit = row.get("exit_code")
            if isinstance(raw_exit, int) and not isinstance(raw_exit, bool):
                exit_code = raw_exit
            minutes: int | None = None
            if outcome in ("timeout", "stalled"):
                job_type = row.get("job_type")
                if isinstance(job_type, str):
                    key = (
                        "timeout_minutes"
                        if outcome == "timeout"
                        else "stall_minutes"
                    )
                    minutes = finalize.budget(runner_config, job_type, key)
            body = outcome_body(
                job_id, outcome, exit_code=exit_code, minutes=minutes
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
        except Exception:
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
