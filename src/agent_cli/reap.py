"""Decide which worktrees and tmux sessions are orphaned after a worker dies.

Pure module: no subprocess, no network, no filesystem, no Store. Once workers can be
killed for running over time or stalling, stale artefacts remain — worktrees whose job
no longer runs, tmux sessions with no corresponding row, half-finished clone directories.
This module answers "what is orphaned" so the caller can act; it deletes nothing.
"""

from __future__ import annotations

import posixpath


PREPARING_MAX_AGE = 300


def list_sessions_argv(socket: str) -> list[str]:
    """tmux list-sessions argv that reports every session name for the given socket."""
    return ["tmux", "-S", socket, "list-sessions", "-F", "#{session_name}"]


def preparing_active(
    present: bool, mtime: int | None, now_epoch: int, *, max_age: int = PREPARING_MAX_AGE
) -> bool:
    """True when a preparing marker still protects its worktree from reaping."""
    if not present:
        return False
    if mtime is None:
        # Marker exists but its age is unreadable; treat it as active, because
        # failing the other way would reap a worktree being built right now.
        return True
    age = max(0, now_epoch - mtime)
    if age >= max_age:
        # An abandoned dispatch must not protect a real orphan forever — that is
        # why the marker carries an age.
        return False
    return True


def within_root(root: str, candidate: str) -> bool:
    """True only when candidate is strictly below root."""
    if not isinstance(root, str) or not isinstance(candidate, str):
        return False
    root_n = posixpath.normpath(root)
    cand_n = posixpath.normpath(candidate)
    if not root_n.startswith("/") or not cand_n.startswith("/"):
        return False
    if cand_n == root_n:
        # The root itself must never be deletable. Type-defensive rather than
        # behaviour-changing: the prefix test below already excludes equality,
        # so removing this line changes no outcome and a mutation of it stays
        # green. It stays because the rule is worth stating where a reader
        # looks for it, rather than leaving it implied by a string comparison.
        return False
    # LIMIT: this is a *lexical* check. The original resolved symlinks first before
    # comparing; a pure module cannot, so the caller must pass already-resolved paths.
    # Without that, a symlink inside the root could point outside it and this guard
    # would not notice. Being wrong here deletes data.
    return cand_n.startswith(root_n + "/")


def orphan_worktrees(work_dirs: list[str], running_ids: set[str], preparing_ids: set[str]) -> list[str]:
    """Entries of work_dirs that are in neither running_ids nor preparing_ids.

    Input order is preserved.
    """
    result: list[str] = []
    for d in work_dirs:
        if d not in running_ids and d not in preparing_ids:
            result.append(d)
    return result


def orphan_sessions(
    session_names: list[str], running_sessions: set[str], preparing_ids: set[str], *, prefix: str
) -> list[str]:
    """Entries of session_names that start with prefix, are not in running_sessions,
    and whose job id (name minus prefix) is not in preparing_ids.

    Names without the prefix are ignored entirely — they belong to something else
    and must never be killed. Input order is preserved.
    """
    result: list[str] = []
    for name in session_names:
        if not name.startswith(prefix):
            continue
        if name in running_sessions:
            continue
        job_id = name[len(prefix) :]
        if job_id in preparing_ids:
            continue
        result.append(name)
    return result
