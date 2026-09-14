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
