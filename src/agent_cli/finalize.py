"""Decide how a finished job is recorded and whether the claim is credible.

An agent exiting 0 and writing DONE is a self-report. This module looks for an
independent artefact that the work happened and downgrades the recorded outcome
when that artefact is absent.
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

DONE_KINDS: tuple[str, ...] = ("pr-reviewed", "pr-mergeable", "marker")


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def done_kind(policy: Any, job_type: str) -> str:
    """Return the done_kind configured for job_type, or a derived default."""
    if isinstance(policy, dict):
        skills = policy.get("skills")
        if isinstance(skills, dict):
            skill = skills.get(job_type)
            if isinstance(skill, dict):
                raw = skill.get("done_kind")
                if isinstance(raw, str) and raw:
                    if raw in DONE_KINDS:
                        return raw
                    # Unrecognised setting must degrade to the weakest proof,
                    # never to a convenient one.
                    return "marker"
    # Derive from job type when the policy is silent or unusable.
    if job_type in ("pr-review", "pr-ready"):
        return "pr-reviewed"
    if job_type == "merge-conflict":
        return "pr-mergeable"
    return "marker"


def transcript_has_marker(transcript: str, ref: str) -> bool:
    """Return True if a line starting with DONE names ref from field 3 onward."""
    if not isinstance(transcript, str) or not isinstance(ref, str):
        return False
    # Strip a single leading # so callers may pass "#123" or "123".
    needle = ref[1:] if ref.startswith("#") else ref
    if not needle:
        return False
    for line in transcript.splitlines():
        # Only lines that start with DONE followed by whitespace qualify.
        # Across 61 real transcripts, 57 mention DONE somewhere but only
        # 32 at line start — without the anchor a line like
        # "- None of the lines `DONE review PR 206 …` — gate A did not pass"
        # would be read as success.
        if not line.startswith("DONE\t") and not line.startswith("DONE "):
            continue
        # Split on runs of spaces and tabs; the marker must appear from
        # the third field onward — field 1 is DONE, field 2 is the identifier,
        # so DONE <ref> alone is not a marker. Compare as strings so 01 does
        # not match a ref of 1.
        parts = re.split(r"[ \t]+", line)
        if len(parts) < 3:
            continue
        for part in parts[2:]:
            if part == needle:
                return True
    return False


def judge(
    *,
    outcome: str,
    kind: str,
    kinds: set[str] | None,
    contract_followed: bool,
    mergeable: bool | None = None,
) -> tuple[str, str] | None:
    """Return (final outcome, work_performed) or None if the check is pending."""
    if outcome != "success":
        return (outcome, "n_a")
    if kinds is None:
        # The output check could not be answered; retry rather than record
        # a half-formed conclusion.
        return None
    # The outcome downgrade and work_performed are computed independently:
    # a merge-conflict job that resolves the conflict and pushes without
    # commenting yields ("agent_failed", "yes") — work done by pushing a
    # commit without commenting.
    result = "agent_failed" if not kinds else outcome
    if kind == "pr-reviewed":
        work = "yes" if "review" in kinds else "no"
        return (result, work)
    if kind == "pr-mergeable":
        # Weaker than pr-reviewed: checks the pull request's current state,
        # not authorship, so it can hold without this job having caused it.
        if mergeable is True:
            return (result, "yes")
        if mergeable is False:
            return (result, "no")
        return (result, "unknown")
    if kind == "marker":
        # Weakest evidence, a self-report, recorded as such so the weakness
        # stays visible in the data rather than hidden.
        return (result, "yes" if contract_followed else "no")
    return (result, "unknown")


def finalize_running(
    store: Store,
    runner: Callable[[list[str]], Completed],
    *,
    socket: str,
    repos_root: str,
    policy: Any,
    login: str,
    exit_code_of: Callable[[str], int | None],
    transcript_of: Callable[[str], str],
) -> tuple[list[str], int]:
    """Finalize running jobs whose workers have exited or whose tmux session is gone.

    Returns (finalized_job_ids, skipped_count). A bad row, a missing exit code,
    or an unanswerable judge call ends only that row; the rest remain processable.
    """
    finalized: list[str] = []
    skipped = 0

    for row in store.rows("job"):
        if row.get("state") != "running":
            continue

        job_id = row.get("id")
        repo = row.get("repo")
        ref = row.get("ref")
        job_type = row.get("job_type")
        session = row.get("session")
        baseline_output_ids = row.get("baseline_output_ids")
        started = row.get("started")

        if (
            not isinstance(job_id, str)
            or not job_id.strip()
            or not isinstance(repo, str)
            or not repo.strip()
            or not isinstance(ref, str)
            or not ref.strip()
            or not isinstance(job_type, str)
            or not job_type.strip()
            or not isinstance(session, str)
            or not session.strip()
            or not isinstance(baseline_output_ids, list)
            or not isinstance(started, str)
            or not started.strip()
        ):
            skipped += 1
            continue

        # Timeout and stall detection are not part of this change. Until they
        # land a hung worker stays "running" rather than being wrongly failed.
        exit_code = exit_code_of(job_id)
        if exit_code is not None:
            outcome = "success" if exit_code == 0 else "agent_failed"
        else:
            # No exit file yet: check whether the tmux session is still alive.
            completed = runner(workspace.has_session_argv(socket, session))
            if completed.returncode == 0:
                # Worker is still running; skip for now.
                skipped += 1
                continue
            outcome = "crashed"

        kind = done_kind(policy, job_type)
        contract_followed = transcript_has_marker(transcript_of(job_id), ref)

        if outcome == "success":
            kinds_completed = runner(outputs.kinds_argv(repo, ref))
            kinds: set[str] | None = None
            if kinds_completed.returncode == 0:
                try:
                    payload = json.loads(kinds_completed.stdout)
                except json.JSONDecodeError:
                    kinds = None
                else:
                    kinds = outputs.parse_new_kinds(
                        payload, login=login, baseline=baseline_output_ids, since=started
                    )
        else:
            kinds = None

        # The mergeable probe is not ported yet, so a pr-mergeable job records
        # work_performed="unknown" rather than a guessed value.
        verdict = judge(
            outcome=outcome, kind=kind, kinds=kinds, contract_followed=contract_followed, mergeable=None
        )
        if verdict is None:
            # The check could not be answered: leave the row running and retry.
            # This is the same fail-closed shape as the baseline guard in dispatch.py;
            # recording a verdict nobody can justify would be worse than skipping.
            skipped += 1
            continue

        final_outcome, work_performed = verdict

        # Cleanup order: kill the session first, then remove and prune the worktree.
        # Removing a worktree from under a live worker is when the removal itself fails.
        # Ignore exit codes — a failing cleanup must not mask the result.
        bare = workspace.bare_path(repos_root, repo)
        worktree = row.get("worktree")
        runner(workspace.kill_session_argv(socket, session))
        if isinstance(worktree, str) and worktree.strip():
            runner(workspace.worktree_remove_argv(bare, worktree))
            runner(workspace.worktree_prune_argv(bare))

        updated = _strip(row)
        updated.update(
            {
                "state": "done" if final_outcome == "success" else "failed",
                "outcome": final_outcome,
                "exit_code": exit_code,
                "done_kind": kind,
                "work_performed": work_performed,
                "contract_followed": "yes" if contract_followed else "no",
                "finished": utcnow(),
                "updated_at": utcnow(),
            }
        )
        try:
            store.write("job", "update", job_id, updated)
            finalized.append(job_id)
        except Exception:
            skipped += 1

    return finalized, skipped
