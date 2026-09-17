"""Report how many jobs sit in each queue state, and decide which finished
jobs are old enough to drop. Pure module: no subprocess, no network, no
filesystem, no Store. Callers supply the rows and the clock; this module
only applies the rules.

The existing runner did both jobs against a filesystem queue and used a
lock dance to survive races. The store this port uses has row-level
consistency, so only the rules are carried over, not the locking.
"""

from __future__ import annotations

from typing import Any

from .watchdog import iso_epoch


def state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How many jobs sit in each state.

    Count each row's `state` value. Rows that are not dicts, and dicts
    whose `state` is not a non-empty string, are ignored. Only states
    actually present in the input appear in the result.
    """
    if not isinstance(rows, list):
        return {}
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = row.get("state")
        if not isinstance(state, str) or not state:
            continue
        counts[state] = counts.get(state, 0) + 1
    return counts


def retention_days(runner_config: Any, state: str) -> int | None:
    """Look up `runner_config["retention_days"][state]` defensively.

    Return the value only when it is a non-negative int (`bool` is
    rejected). Any missing or wrong-typed level of the path yields None.
    """
    if not isinstance(runner_config, dict) or not isinstance(state, str):
        return None
    table = runner_config.get("retention_days")
    if not isinstance(table, dict):
        return None
    value = table.get(state)
    # A missing or malformed setting must mean "do not prune", never
    # "prune everything": treating absence as 0 would delete every
    # matching row the first time the cleaner ran.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def prunable(rows: list[dict[str, Any]], *, state: str, now_epoch: int, days: int) -> list[str]:
    """Ids of rows in `state` whose `finished` time is strictly older
    than `now_epoch - days * 86400`.

    Skip — never prune — a row whose `id` is not a non-empty string,
    whose `finished` field is missing, or whose `finished` field does
    not parse via `iso_epoch`. Wrong-shaped input yields an empty list.
    `days` must be a non-negative int (`bool` is rejected). Strictly
    less than.
    """
    if not isinstance(rows, list):
        return []
    if not isinstance(now_epoch, int) or isinstance(now_epoch, bool):
        return []
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        return []
    cutoff = now_epoch - days * 86400
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("state") != state:
            continue
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id:
            continue
        finished = row.get("finished")
        # A job whose completion time cannot be established must not be
        # deleted on a guess.
        if not isinstance(finished, str):
            continue
        stamp = iso_epoch(finished)
        if stamp is None:
            continue
        if stamp < cutoff:
            ids.append(job_id)
    return ids


def retry_payload(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return a new queued row with the previous attempt erased, or None.

    `row` is not mutated. Return None when `row` is not a dict or its
    `id` is not a non-empty string.

    Caller obligation: the caller must delete the previous attempt's
    exit-code and transcript artefacts before writing this returned row
    back to storage. Otherwise the supervisor reads the old exit code
    on its next pass and finalises the retry instantly with the
    previous attempt's outcome. This is a caller obligation because
    this is a pure module (no filesystem access).
    """
    if not isinstance(row, dict):
        return None
    job_id = row.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None
    attempts = row.get("attempts")
    # bool is a subclass of int, so True/False must not count as a tally.
    if not isinstance(attempts, int) or isinstance(attempts, bool):
        attempts = 0
    out = dict(row)
    # The existing runner needed two separate steps to do this safely —
    # reset the row's fields while it was still in the failed state, then
    # move it to queued in a second step — so that a partially reset job
    # could never be observed in the queued state. Writing the whole row
    # at once, as this function does, makes that two-step ordering
    # unnecessary here.
    out["state"] = "queued"
    out["attempts"] = attempts + 1
    for key in (
        "started",
        "session",
        "worktree",
        "finished",
        "exit_code",
        "outcome",
        "done_kind",
        "work_performed",
        "contract_followed",
    ):
        out[key] = None
    # A retried job has to announce itself again, and its previous
    # outcome must not be treated as already reported. Skipping this
    # makes a retry invisible.
    out["reported"] = False
    out["announced"] = False
    # A missing progress field is a first baseline for the stall check;
    # leaving None behind would be a value that check would have to
    # special-case instead.
    for key in (
        "progress_check_epoch",
        "progress_transcript_size",
        "progress_cpu_seconds",
    ):
        out.pop(key, None)
    return out
