"""Decide which worktrees and tmux sessions are orphaned after a worker dies.

Pure module: no subprocess, no network, no filesystem, no Store. Once workers can be
killed for running over time or stalling, stale artefacts remain — worktrees whose job
no longer runs, tmux sessions with no corresponding row, half-finished clone directories.
This module answers "what is orphaned" so the caller can act; it deletes nothing.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable

from .runtime import Completed
from .store import Store
from . import workspace


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


def reap_orphans(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    socket: str,
    repos_root: str,
    work_root: str,
    work_dirs: list[str],
    marker_of: Callable[[str], tuple[bool, int | None]],
    now_epoch: int,
    session_prefix: str,
) -> tuple[list[str], list[str], int]:
    """Reap orphaned worktrees and tmux sessions after workers are killed."""
    removed_worktrees: list[str] = []
    killed_sessions: list[str] = []
    skipped = 0

    # Step 1: collect running job ids and session names
    running_ids: set[str] = set()
    running_sessions: set[str] = set()
    for row in store.rows("job"):
        if row.get("state") != "running":
            continue
        job_id = row.get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            continue
        running_ids.add(job_id)
        session = row.get("session")
        if isinstance(session, str) and session.strip():
            running_sessions.add(session)

    # Step 2: list sessions first (needed for candidate ids and to decide whether to sweep)
    list_result = runner(list_sessions_argv(socket))
    # "Cannot list" is not "nothing is running": a failed listing must kill nothing,
    # and this flag is the single guard that enforces it. The names are parsed either
    # way so that guard stands alone and can be exercised, rather than being masked by
    # an empty list that would hide its removal.
    session_listing_failed = list_result.returncode != 0
    candidate_session_names: list[str] = (
        list_result.stdout.splitlines() if list_result.stdout else []
    )

    # Step 3: build preparing_ids from work_dirs AND session-derived job ids
    # A job can hold a preparing marker before or without its work directory existing;
    # killing its session would abandon a dispatch mid-flight.
    preparing_ids: set[str] = set()
    candidate_job_ids: set[str] = set(work_dirs)
    for name in candidate_session_names:
        if name.startswith(session_prefix):
            candidate_job_ids.add(name[len(session_prefix) :])
    for job_id in candidate_job_ids:
        present, mtime = marker_of(job_id)
        if preparing_active(present, mtime, now_epoch):
            preparing_ids.add(job_id)

    # Step 4: worktrees
    for job_id in orphan_worktrees(work_dirs, running_ids, preparing_ids):
        row = store.row("job", job_id)
        if row is None:
            # no row knows about this directory — needs a raw delete, not part of this change
            skipped += 1
            continue
        repo = row.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            skipped += 1
            continue
        path = f"{work_root}/{job_id}"
        try:
            bare = workspace.bare_path(repos_root, repo)
        except ValueError:
            skipped += 1
            continue
        if not within_root(work_root, path):
            # guard against traversal: a job id containing .. would let the path escape the work root
            skipped += 1
            continue
        runner(workspace.worktree_remove_argv(bare, path))
        runner(workspace.worktree_prune_argv(bare))
        removed_worktrees.append(job_id)

    # Step 5: sessions (skip entirely if listing failed)
    if not session_listing_failed:
        for name in orphan_sessions(candidate_session_names, running_sessions, preparing_ids, prefix=session_prefix):
            runner(workspace.kill_session_argv(socket, name))
            killed_sessions.append(name)

    return removed_worktrees, killed_sessions, skipped
