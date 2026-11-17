"""Take a queued job and prepare its workspace for a detached worker.

Phase 3 of porting the existing runner. Every external effect (store, runner)
is injected; this module only decides the sequence and the payloads. The
original runner fixed the order announcement → output baseline → started →
worker start. The announcement is injected as a callback and is called before
the baseline is captured, so the baseline already holds the bot's own
comment. Taken before the announcement, the baseline would miss that comment,
the later silent-failure check would count it as new output, and every job
would pass.

The worktree holds the code the job is about: the pull-request head, checked
out detached, for a pull-request job, and the default branch for an issue job.
The stored `ref` stays the issue/PR number — the output queries need it — so it
is never a branch name. Whether the number names a pull request is asked from
GitHub first; an unanswered question leaves the job queued rather than
guessing, because checking out the default branch for a pull request would
review the wrong code. Pushing back to a pull-request branch is not part of
this change.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from . import outputs
from . import workspace
from .runtime import Completed
from .store import Store, utcnow


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def worktree_path(work_root: str, job_id: str) -> str:
    """Absolute worktree path for the given job under `work_root`."""
    return f"{work_root}/{job_id}"


SESSION_PREFIX = "job-"


def session_name(job_id: str) -> str:
    """tmux session name for the given job: the whole id, never truncated.

    `reap_orphans` rebuilds the name from the row id and compares it against a
    listing of live sessions, so the mapping must be injective. Replacing
    unsafe characters keeps it so; truncating (as a generic name helper does)
    would make two long ids share a session name and let a live job be reaped.
    """
    return SESSION_PREFIX + re.sub(r"[^A-Za-z0-9_-]", "-", job_id)


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
    announce: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[list[str], int]:
    """Start as many queued jobs as capacity allows.

    Returns (started_ids, skipped_count). A bad row or a failed runner call
    ends only that one job; the rest of the queue stays processable.

    `announce` posts the "working on this" comment. It runs right after the
    worktree exists and before the output baseline, and a failure or an
    exception does not stop the job: the original runner started the work
    regardless. The returned bool says whether the comment was delivered, so
    the row can record that it was announced and a later pass does not post a
    second one.
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

        # `ref` is the issue or pull-request number, never a branch. Ask what
        # it names before choosing a commit: an unanswered probe is not "it is
        # an issue", because checking out the default branch for a pull request
        # would hand the worker unrelated code. The row stays queued either way.
        try:
            probe = runner(workspace.pull_request_probe_argv(repo, ref))
        except ValueError:
            # A malformed `ref` cannot be looked up; the row is skipped like
            # the other unusable rows above.
            skipped += 1
            continue
        answer = (probe.stdout or "").strip()
        if probe.returncode != 0 or answer not in ("true", "false"):
            skipped += 1
            continue

        if answer == "true":
            number = ref[1:] if ref.startswith("#") else ref
            pull = runner(workspace.fetch_pull_argv(bare, ref))
            if pull.returncode != 0:
                skipped += 1
                continue
            add = runner(
                workspace.worktree_add_ref_argv(
                    bare, worktree, f"refs/remotes/origin/pull/{number}"
                )
            )
        else:
            default = runner(workspace.default_branch_argv(repo))
            branch = (default.stdout or "").strip()
            if default.returncode != 0 or not branch or "\n" in branch:
                skipped += 1
                continue
            add = runner(workspace.worktree_add_argv(bare, worktree, branch))

        if add.returncode != 0:
            updated = _strip(row)
            updated.update(
                {
                    "state": "failed",
                    "outcome": "workspace_failed",
                    "finished": utcnow(),
                    # `supervise` reports this failure, and the target is
                    # already known from the probe.
                    "pull_request": answer == "true",
                    "reported": False,
                }
            )
            store.write("job", "update", job_id, updated)
            skipped += 1
            continue

        # The worktree exists now, so the row can record what the probe said
        # and the announcement can be posted against the right target.
        row["pull_request"] = answer == "true"

        # The announcement must come before the output baseline, so the
        # baseline already holds this instance's own comment. A baseline taken
        # first would miss it, and the later silent-failure check would count
        # the comment as new output and pass every job.
        if announce is not None and row.get("announced") is not True:
            try:
                announced = announce(row)
            except Exception:
                # A broken announcement must not stop work the user asked for.
                announced = False
            if announced:
                # Write at once: a later baseline failure leaves the job
                # queued, and the next pass must not announce it again. If this
                # write fails, carry on — the deterministic activity id already
                # prevents a second comment.
                row["announced"] = True
                try:
                    store.write("job", "update", job_id, _strip(row))
                except Exception:
                    pass

        completed = runner(outputs.baseline_argv(repo, ref))
        baseline_output_ids = None
        if completed.returncode == 0:
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                pass
            else:
                baseline_output_ids = outputs.parse_baseline_ids(payload)
        if baseline_output_ids is None:
            # An untrusted baseline must not start a worker: a later phase
            # compares against it to decide whether work really happened.
            # The row stays queued so the next pass retries.
            runner(workspace.worktree_remove_argv(bare, worktree))
            runner(workspace.worktree_prune_argv(bare))
            skipped += 1
            continue

        started_ts = utcnow()

        session = session_name(job_id)
        start = runner(workspace.new_session_argv(socket, session, worktree, wrapper))
        if start.returncode != 0:
            runner(workspace.worktree_remove_argv(bare, worktree))
            runner(workspace.worktree_prune_argv(bare))
            updated = _strip(row)
            updated.update(
                {
                    "state": "failed",
                    "outcome": "crashed",
                    "finished": utcnow(),
                    "reported": False,
                }
            )
            store.write("job", "update", job_id, updated)
            skipped += 1
            continue

        updated = _strip(row)
        updated.update(
            {
                "state": "running",
                "session": session,
                "worktree": worktree,
                "baseline_output_ids": baseline_output_ids,
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
