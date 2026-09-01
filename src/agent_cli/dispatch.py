"""Take a queued job and prepare its workspace for a detached worker.

Phase 3 of porting the existing runner. Every external effect (store, runner)
is injected; this module only decides the sequence and the payloads. The
original runner fixed the order announcement → output baseline → started →
worker start. Announcement and baseline need network and belong in a later
phase. Immediately above `started = utcnow()`, the comment below records that
they must come before it: a baseline taken after the announcement would count
the bot's own comment as new output and make the later silent-failure check
pass every job.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import runtime
from . import workspace
from .runtime import Completed
from .store import Store, utcnow


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def worktree_path(work_root: str, job_id: str) -> str:
    """Absolute worktree path for the given job under `work_root`."""
    return f"{work_root}/{job_id}"


def session_name(job_id: str) -> str:
    """tmux session name for the given job."""
    return runtime.tmux_name(job_id)


def running_slots(store: Store, runner: Callable[[list[str]], Completed], *, socket: str) -> int:
    """Count how many running jobs still have a live tmux session."""
    count = 0
    for row in store.rows("job"):
        if row.get("state") != "running":
            continue
        session = row.get("session")
        if not session:
            # orphan row with no session occupies no slot
            continue
        completed = runner(workspace.has_session_argv(socket, session))
        if completed.returncode == 0:
            count += 1
    return count


def dispatch_queued(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    socket: str,
    repos_root: str,
    work_root: str,
    wrapper: str,
    max_concurrent: int,
) -> tuple[list[str], int]:
    """Start as many queued jobs as capacity allows.

    Returns (started_ids, skipped_count). A bad row or a failed runner call
    ends only that one job; the rest of the queue stays processable.
    """
    if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or max_concurrent < 1:
        raise ValueError("max_concurrent must be a positive int")

    started_ids: list[str] = []
    skipped = 0

    for row in store.rows("job"):
        if row.get("state") != "queued":
            continue

        if running_slots(store, runner, socket=socket) >= max_concurrent:
            break

        repo = row.get("repo")
        ref = row.get("ref")
        job_type = row.get("job_type")
        attempts = row.get("attempts")
        job_id = row.get("id")

        if (
            not isinstance(repo, str)
            or not repo.strip()
            or not isinstance(ref, str)
            or not ref.strip()
            or not isinstance(job_type, str)
            or not job_type.strip()
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not isinstance(job_id, str)
            or not job_id.strip()
        ):
            skipped += 1
            continue

        bare = workspace.bare_path(repos_root, repo)

        # ensure bare repository exists
        is_bare = runner(workspace.is_bare_argv(bare))
        if not (is_bare.returncode == 0 and is_bare.stdout.strip() == "true"):
            clone = runner(workspace.clone_argv(repo, bare))
            if clone.returncode != 0:
                skipped += 1
                continue

        # refspec + fetch run on every job, whether the bare repo was just
        # cloned or already existed
        refspec = runner(workspace.fetch_refspec_argv(bare))
        if refspec.returncode != 0:
            skipped += 1
            continue
        fetch = runner(workspace.fetch_argv(bare))
        if fetch.returncode != 0:
            skipped += 1
            continue

        worktree = worktree_path(work_root, job_id)
        add = runner(workspace.worktree_add_argv(bare, worktree, ref))
        if add.returncode != 0:
            updated = _strip(row)
            updated.update({"state": "failed", "outcome": "workspace_failed", "finished": utcnow()})
            store.write("job", "update", job_id, updated)
            skipped += 1
            continue

        # Announcement and output baseline belong BEFORE this line.
        # A baseline taken after the announcement would count the bot's own
        # comment as new output and make the later silent-failure check pass
        # every job.
        started_ts = utcnow()

        session = session_name(job_id)
        start = runner(workspace.new_session_argv(socket, session, worktree, wrapper))
        if start.returncode != 0:
            runner(workspace.worktree_remove_argv(bare, worktree))
            runner(workspace.worktree_prune_argv(bare))
            updated = _strip(row)
            updated.update({"state": "failed", "outcome": "crashed", "finished": utcnow()})
            store.write("job", "update", job_id, updated)
            skipped += 1
            continue

        updated = _strip(row)
        updated.update(
            {
                "state": "running",
                "session": session,
                "worktree": worktree,
                "started": started_ts,
                "updated_at": utcnow(),
            }
        )
        try:
            store.write("job", "update", job_id, updated)
            started_ids.append(job_id)
        except Exception:
            # The row never reached running, so a leftover worktree would make
            # the next pass find the path already present and fail the job.
            # Kill the session first so the worktree is not pulled out from
            # under a live worker.
            runner(workspace.kill_session_argv(socket, session))
            runner(workspace.worktree_remove_argv(bare, worktree))
            runner(workspace.worktree_prune_argv(bare))
            skipped += 1

    return started_ids, skipped
