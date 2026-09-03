"""Decide whether a tmux worker pane has run past its time budget or has
stalled, from caller-supplied measurements. Pure module: no subprocess, no
network, no filesystem, no Store. It builds the tmux/ps argument vectors a
caller will run, and parses the text those commands return.

The existing runner already collects pane PIDs, a ps snapshot, transcript size,
CPU seconds, and last-check metadata. This module supplies the decision logic
that a later change will wire in.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def pane_pids_argv(socket: str, session: str) -> list[str]:
    """tmux list-panes argv that reports the exact session's pane PIDs.

    The `=` prefix on `-t` is load-bearing: tmux treats `=name` as an exact
    match. Without it the command would match a prefix, so one session could be
    reported under another's name — the same rule as workspace.py.
    """
    return ["tmux", "-S", socket, "list-panes", "-t", f"={session}", "-F", "#{pane_pid}"]


def ps_snapshot_argv() -> list[str]:
    """ps argv that emits pid, ppid and cumulative CPU time for every process."""
    return ["ps", "-axo", "pid=,ppid=,time="]


def parse_pane_pids(text: str) -> list[str]:
    """Return non-empty, whitespace-stripped lines from tmux output, in order."""
    if not isinstance(text, str):
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines


def cpu_seconds(value: str) -> int | None:
    """Parse one ps TIME field to whole seconds.

    Drop any fractional part after `.`. An optional `DD-` prefix supplies days;
    the remainder is HH:MM:SS, MM:SS or SS. Any other field count yields None.
    The original forced base-10 parsing because `08` would otherwise read as
    octal; Python's `int()` has no such problem, so the guard's absence here is
    deliberate.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    # Drop fractional seconds if present.
    if "." in value:
        value = value.split(".", 1)[0]
    days = 0
    if "-" in value:
        parts = value.split("-", 1)
        if len(parts) != 2 or not parts[0].isdigit():
            return None
        days = int(parts[0])
        value = parts[1]
    segs = value.split(":")
    if len(segs) not in (1, 2, 3):
        return None
    try:
        nums = [int(s) for s in segs]
    except ValueError:
        return None
    if len(nums) == 1:
        secs = nums[0]
        mins = 0
        hours = 0
    elif len(nums) == 2:
        secs = nums[1]
        mins = nums[0]
        hours = 0
    else:
        secs = nums[2]
        mins = nums[1]
        hours = nums[0]
    return days * 86400 + hours * 3600 + mins * 60 + secs


def tree_cpu_seconds(snapshot: str, pane_pids: list[str]) -> int | None:
    """Sum CPU seconds over the pane processes and all their descendants.

    Each snapshot line is `pid ppid time`. Lines that do not split into three
    fields are skipped. The selected set is seeded from `pane_pids`, then any
    pid whose ppid is already selected is added, iterating to a fixed point —
    a grandchild may appear before its parent in the snapshot, so one pass is
    not enough. Return None, never 0, when no selected pid appears at all or
    when any selected pid's time field is unparseable: 0 would mean "measured,
    used no CPU", None means "could not measure", and a caller treating None
    as 0 would see a vanished session as making no progress and kill it as
    stalled.
    """
    if not isinstance(snapshot, str) or not isinstance(pane_pids, list):
        return None
    if not pane_pids:
        return None
    # Build pid -> (ppid, time_str) map, skipping malformed lines.
    proc: dict[str, tuple[str, str]] = {}
    for line in snapshot.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, t = parts
        proc[pid] = (ppid, t)
    # Seed the working set from the pane PIDs that actually exist.
    selected: set[str] = set()
    for p in pane_pids:
        if p in proc:
            selected.add(p)
    if not selected:
        return None
    # Iterate to fixed point: add children of any already-selected pid.
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in proc.items():
            if ppid in selected and pid not in selected:
                selected.add(pid)
                changed = True
    total = 0
    for pid in selected:
        _, t = proc[pid]
        secs = cpu_seconds(t)
        if secs is None:
            return None
        total += secs
    return total


def iso_epoch(stamp: str) -> int | None:
    """Parse YYYY-MM-DDTHH:MM:SSZ to a Unix timestamp, or None on failure."""
    if not isinstance(stamp, str):
        return None
    try:
        dt = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def is_overdue(started: str, now_epoch: int, timeout_minutes: int) -> bool | None:
    """True when now_epoch - iso_epoch(started) > timeout_minutes * 60.

    False when the difference is <= the budget. None when started does not
    parse or timeout_minutes is not a non-negative int (bool is rejected).
    Strictly greater than.
    """
    if not isinstance(now_epoch, int) or not isinstance(timeout_minutes, int):
        return None
    if isinstance(timeout_minutes, bool) or timeout_minutes < 0:
        return None
    start = iso_epoch(started)
    if start is None:
        return None
    return (now_epoch - start) > (timeout_minutes * 60)


def stall_verdict(
    *,
    now_epoch: int,
    transcript_size: int,
    cpu: int,
    last_check: object,
    last_size: object,
    last_cpu: object,
    stall_minutes: int,
) -> str:
    """Return one of {"baseline", "wait", "stalled", "progress"}.

    "baseline" when any of last_check, last_size, last_cpu is not a non-negative
    int (bool rejected). "wait" when the stall window has not yet elapsed.
    "stalled" when transcript_size <= last_size AND cpu <= last_cpu.
    "progress" otherwise.

    Both signals are required because a worker can be quiet in its transcript
    while still computing, and can burn no CPU while waiting on the network —
    either alone would kill live work.
    """
    for v in (last_check, last_size, last_cpu):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            return "baseline"
    if not isinstance(now_epoch, int) or not isinstance(transcript_size, int) or not isinstance(cpu, int):
        return "baseline"
    if not isinstance(stall_minutes, int) or isinstance(stall_minutes, bool) or stall_minutes < 0:
        return "baseline"
    if (now_epoch - last_check) < (stall_minutes * 60):
        return "wait"
    if transcript_size <= last_size and cpu <= last_cpu:
        return "stalled"
    return "progress"
