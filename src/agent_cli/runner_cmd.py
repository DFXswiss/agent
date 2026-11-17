"""Operate the runner: ingest mentions, dispatch, supervise, prune and retry.

The pure modules (`jobs`, `ingest`, `dispatch`, `finalize`, `reap`, `queue`,
`health`) decide what should happen. This module is the command layer: it reads
the runner's configuration files, opens the store, runs the real `gh`, `git`,
`tmux` and `ps` through `runtime.run_argv`, and carries out what those decisions
require.

Every mutating subcommand takes one non-blocking lock on `state/runner.lock`.
The store keeps rows consistent, but a dispatch is a sequence of external steps
(worktree add, output baseline, tmux start, row update). A `reap_orphans` pass
running at the same time would see the half-built worktree of a still-`queued`
job as an orphan and remove it. Serialising the mutating commands makes that
impossible, which is also why no "preparing" marker is ever written:
`reap_orphans` receives `marker_of` that always answers "absent".

`work` is the exception: it is the detached worker for one job and runs for the
whole job. Taking the runner lock there would either print `runner busy` and
leave the job unfinished or hold the whole queue while one lane runs. Dispatch
starts the worker immediately before it writes the row as running, so the worker
waits for that update before it validates the row. The worker itself changes no
worktree, so it races with no mutating command.

`main` imports this module at the top, so the helpers from `main` are imported
inside the functions that use them — a module-level import would be circular.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import dispatch, finalize, health, ingest, queue, reap, workspace
from .runtime import Completed, run_argv
from .store import Store

USAGE = (
    "Usage: agent runner ingest --session ID|dispatch|work|supervise|prune|"
    "retry JOB_ID|status|doctor"
)

_EXIT_CODE_RE = re.compile(r"^-?[0-9]+$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def safe_job_id(value: Any) -> bool:
    """True when `value` may be used as a job id, and therefore as a path component.

    Job ids become file names (`state/logs/<id>.exit`, `state/work/<id>`) and
    worktree paths, but the store accepts any string as a row id. A row whose id
    is `/tmp/x` or `../x` would make prune unlink a file outside the logs
    directory, retry remove a path outside the work root, and dispatch create a
    worktree outside it. Only a plain name is accepted: `.` and `/` are excluded,
    so `.`/`..` and every path separator are impossible.
    """
    return isinstance(value, str) and _SAFE_JOB_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class RunnerPaths:
    """Every path the runner commands use, derived from one base directory."""

    base: Path
    policy: Path
    runner_config: Path
    skills_catalog: Path
    agent_config: Path
    prompts: Path
    state: Path
    socket: Path
    repos: Path
    work: Path
    logs: Path
    lock: Path


def runner_paths(base: Path) -> RunnerPaths:
    """Derive the fixed runner layout from its base directory.

    There is no fallback identity: every command uses exactly these paths, and
    a test can point the whole group at a temporary directory by passing a
    different `base`.
    """
    state = base / "state"
    return RunnerPaths(
        base=base,
        policy=base / "policy.json",
        runner_config=base / "runner.json",
        skills_catalog=base / "skills-catalog.json",
        agent_config=base / "agent.json",
        prompts=base / "prompts",
        state=state,
        socket=state / "tmux.sock",
        repos=state / "repos",
        work=state / "work",
        logs=state / "logs",
        lock=state / "runner.lock",
    )


def _read_json(path: Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read one JSON object, returning `(value, problem)` instead of exiting.

    `doctor` must report every problem it can find and cannot stop at the first
    one, while the acting commands must stop. Both share this reader so the two
    cannot disagree about what a readable object is.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return None, f"{label} {path}: {exc}"
    except OSError as exc:
        return None, f"{label} {path}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, f"{label} {path}: invalid JSON"
    if not isinstance(data, dict):
        return None, f"{label} {path}: not a JSON object"
    return data, None


def load_json(path: Path, label: str) -> dict[str, Any]:
    """Read a configuration object or stop. No defaults, no empty substitution."""
    from .main import die

    data, problem = _read_json(path, label)
    if problem is not None:
        die(problem)
    if data is None:
        # Unreachable: a missing value always carries a problem. It keeps the
        # return type honest without inventing an empty configuration.
        raise AssertionError("unreachable: no value without a problem")
    return data


def expected_login(policy: dict[str, Any]) -> str:
    """The GitHub login this runner must act as, from the policy.

    Absence is fatal rather than empty: a runner acting under whatever identity
    `gh` happens to carry is not the runner the policy describes.
    """
    from .main import die

    raw = policy.get("expected_gh_user") if isinstance(policy, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        die("policy expected_gh_user is missing")
    return raw.strip()


def verify_gh_login(runner: Callable[[list[str]], Completed], expected: str) -> str:
    """Stop unless `gh` is authenticated as `expected`; return the login to use.

    Fail closed: a runner under the wrong identity must not ingest or supervise
    anything, because every action it takes would be attributed to that other
    account.

    The match is case-insensitive, but the returned spelling is exactly what
    `gh api user` printed. `outputs._is_new` compares an output's author login
    case-sensitively against the value handed down, so using the policy's
    spelling (`Example-Bot`) where GitHub says `example-bot` would reject every
    real output and record performed work as failed. Callers pass the return
    value on, not the policy's spelling.
    """
    from .main import die

    try:
        completed = runner(["gh", "api", "user", "--jq", ".login"])
    except OSError:
        # gh's own diagnostic can name a token or a configuration path; the
        # failure alone is the actionable fact.
        die("gh api user failed")
    actual = (completed.stdout or "").strip()
    if completed.returncode != 0:
        # Never copy stderr here: it is an external diagnostic, not a message
        # this command owns, and it may contain credentials.
        die("gh api user failed")
    if actual.lower() != expected.lower():
        # The login came from `gh`, so naming it is safe and says which account
        # is actually selected. An empty answer is reported as unknown.
        die(f"gh is authenticated as {actual or 'unknown'}, policy expects {expected}")
    return actual


class OwnJobs:
    """A Store view restricted to this device's job rows.

    `Store.rows` returns rows from every device, and writing another device's
    row raises. A command that dispatches, finalizes, prunes or retries must
    act only on its own jobs, so the pure functions receive this view. Other
    tables pass through unchanged.

    Rows whose id is not a plain name (`safe_job_id`) are dropped as well. Every
    command that touches the filesystem goes through this view, so an id the
    store accepted but that cannot be a path component never reaches a pure
    module and never becomes a file name. `ingest` deliberately uses the plain
    store: it writes rows only with ids from `jobs.job_id`.

    With `work_root` given, a row whose stored `worktree` is set to anything
    but `<work_root>/<id>` is dropped too. `finalize_running` hands that field
    to `git worktree remove --force`, and the field is data, not a path this
    command derived: only the exact path dispatch writes may reach git.
    """

    def __init__(self, store: Store, *, work_root: Path | None = None) -> None:
        self._store = store
        self._device_id = store.device_id()
        self._work_root = work_root

    def _usable(self, row: dict[str, Any]) -> bool:
        if row.get("_origin_device_id") != self._device_id:
            return False
        job_id = row.get("id")
        if not safe_job_id(job_id):
            return False
        if self._work_root is None:
            return True
        worktree = row.get("worktree")
        return worktree is None or worktree == f"{self._work_root}/{job_id}"

    def rows(self, table: str) -> list[dict[str, Any]]:
        rows = self._store.rows(table)
        if table != "job":
            return rows
        return [row for row in rows if self._usable(row)]

    def row(self, table: str, row_id: str) -> dict[str, Any] | None:
        if table == "job" and not safe_job_id(row_id):
            return None
        row = self._store.row(table, row_id)
        if row is None:
            return None
        if table == "job" and not self._usable(row):
            return None
        return row

    def write(self, table: str, op: str, row_id: str, payload: dict[str, Any]) -> Any:
        return self._store.write(table, op, row_id, payload)


@contextmanager
def runner_lock(paths: RunnerPaths) -> Iterator[bool]:
    """Exclusive, non-blocking lock shared by every mutating subcommand.

    Yields True when acquired and False when another runner command holds it.
    The caller then prints `runner busy` and returns normally: the schedule
    simply tries again. The handle stays open for the command's lifetime.
    """
    paths.state.mkdir(parents=True, exist_ok=True)
    handle = paths.lock.open("w")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        handle.write(str(os.getpid()))
        handle.flush()
        yield True
    finally:
        handle.close()


def _ensure_state_dirs(paths: RunnerPaths) -> None:
    """Create only the runtime directories; configuration files stay absent."""
    for path in (paths.state, paths.repos, paths.work, paths.logs):
        path.mkdir(parents=True, exist_ok=True)


def _remove_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _max_concurrent(runner_config: dict[str, Any]) -> int:
    from .main import die

    value = runner_config.get("max_concurrent")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        die("runner config max_concurrent must be a positive integer")
    return value


def _agent_home() -> Path:
    """`home()` made absolute, without resolving symlinks.

    Every path the runner hands to git or tmux derives from this. A job's
    worktree is another directory than the command that dispatched it, so a
    relative `AGENT_HOME` would name two different places.
    """
    from .main import home

    return Path(os.path.abspath(home()))


def _runner_base() -> RunnerPaths:
    """The runner layout under the absolute agent home."""
    return runner_paths(_agent_home() / "runner")


def exit_code_of(paths: RunnerPaths, job_id: str) -> int | None:
    """The worker's exit code, or None while there is no complete answer.

    A missing file means "no result yet". A half-written or non-numeric file is
    also "no result yet": guessing a code would record an invented outcome.
    Undecodable bytes are treated the same way — `ValueError` covers
    `UnicodeDecodeError`, and an unreadable file is never a guessed code.
    """
    try:
        text = (paths.logs / f"{job_id}.exit").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    stripped = text.strip()
    if _EXIT_CODE_RE.match(stripped) is None:
        return None
    return int(stripped)


def transcript_of(paths: RunnerPaths, job_id: str) -> str:
    """The transcript as text, replacing undecodable bytes; missing means empty."""
    try:
        return (paths.logs / f"{job_id}.transcript").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""


def transcript_size_of(paths: RunnerPaths, job_id: str) -> int | None:
    """Transcript size in bytes, or None when unreadable.

    The stall watchdog compares sizes; a missing measurement must stay None and
    never become 0, which would read as "measured, no progress".
    """
    try:
        return (paths.logs / f"{job_id}.transcript").stat().st_size
    except OSError:
        return None


def work_dirs(paths: RunnerPaths | Path) -> list[str]:
    """Names of the immediate subdirectories of the work root, sorted.

    Accepts the runner layout or the work root itself, so a caller that already
    holds one does not have to reach through the other. A missing directory is
    an empty answer, not an error: nothing is reaped when nothing can be listed.
    A symlinked entry is not a worktree directory and is not listed — reaping
    it would name a job whose worktree is really somewhere else.
    """
    work = paths.work if isinstance(paths, RunnerPaths) else Path(paths)
    try:
        # `Path.iterdir()` is lazy, so its OSError would surface after the
        # `try` had already been left. `scandir` opens eagerly, which keeps the
        # whole listing inside the handler.
        with os.scandir(work) as entries:
            return sorted(
                entry.name
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        return []


# --------------------------------------------------------------------- ingest


def _cmd_ingest(args: list[str]) -> None:
    from .main import die, open_store, require_flag
    from .supervise import SESSION_RE

    session_id = require_flag(args, "--session")
    if SESSION_RE.match(session_id) is None:
        die("session id may contain only A-Za-z0-9_-")
    paths = _runner_base()
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        _ensure_state_dirs(paths)
        policy = load_json(paths.policy, "policy")
        # What a mention asks for is a catalogue skill, never a job type named
        # in the comment. An unreadable catalogue or a default that names no
        # skill leaves every request undecided, so both stop the pass.
        catalog = load_json(paths.skills_catalog, "skills catalog")
        if ingest.catalog_skills(catalog) is None:
            die("skills catalog is not readable")
        if ingest.default_skill(policy, catalog) is None:
            die("policy default_skill_on_mention is not a skill in the catalogue")
        # Use the spelling GitHub reports: `outputs._is_new` compares an
        # author login case-sensitively, so the policy's spelling would reject
        # this runner's own outputs whenever the two differ in case.
        login = verify_gh_login(run_argv, expected_login(policy))
        store = open_store()
        listing_failed = False
        created: list[str] = []
        requeued: list[str] = []
        skipped = 0
        try:
            def requeue(row: dict[str, Any], payload: dict[str, Any]) -> bool:
                try:
                    return (
                        _requeue_job(paths, store, row, payload, run_argv) is None
                    )
                except Exception:
                    # A refused or raising re-queue leaves the thread unread so
                    # the next pass tries again.
                    return False

            # Plain Store, not OwnJobs: scan_mentions de-duplicates against every
            # device's rows. Reading a foreign row as absent would make its later
            # insert raise "already exists".
            created, requeued, skipped = ingest.scan_mentions(
                store,
                run_argv,
                session_id=session_id,
                login=login,
                policy=policy,
                catalog=catalog,
                device_id=store.device_id(),
                requeue=requeue,
            )
        except ingest.ListingFailed:
            # The notification list is the input to every admission decision.
            # Reporting "queued none" would claim a clean pass over a list that
            # was never read, so this exits non-zero and prints no queue line.
            listing_failed = True
        finally:
            store.close()
    if listing_failed:
        print("ingest failed: notifications listing unusable", file=sys.stderr)
        raise SystemExit(1)
    for job_id in created:
        print(f"job.queued {job_id}")
    for job_id in requeued:
        print(f"job.requeued {job_id}")
    if not created and not requeued:
        print("job.queued none")
    if skipped > 0:
        print(f"ingest skipped {skipped}", file=sys.stderr)


# ------------------------------------------------------------------- dispatch


def _worker_preflight(paths: RunnerPaths) -> None:
    """Stop before the lock unless a dispatched job could actually run.

    A job started here begins a real lane and posts a real review, so every
    input the worker needs must already be present. Checking before the lock
    and the store means a broken installation never leaves a half-created job;
    the checks are the same ones `doctor` reports.

    A policy allows a *queue type*; the catalogue maps it to the skill whose id
    names a worker and a `runner.json` entry. A queue type no skill carries is
    refused here rather than queued and then failed.
    """
    from .ai_accounts import AccountError, load_ai_accounts
    from .main import die
    from .runner_worker import WORKER_JOB_TYPES

    try:
        ai = load_ai_accounts(paths.base.parent, defer_model_configuration=True)
        ai.require_model_configuration()
        ai.require_lane_execution()
    except AccountError as exc:
        die(f"dispatch: AI configuration cannot start lanes: {exc}")
    policy = load_json(paths.policy, "policy")
    catalog = load_json(paths.skills_catalog, "skills catalog")
    if ingest.catalog_skills(catalog) is None:
        die("skills catalog is not readable")
    allowed = policy.get("job_types_allow")
    if not isinstance(allowed, list):
        # Admission already treats a missing list as "admits nothing"; there is
        # no allowed type for this command to reject.
        return
    skills, unknown = ingest.allowed_skills(policy, catalog)
    if unknown:
        die(f"job type {unknown[0]} is not in the catalogue")
    for skill in skills:
        if skill.id.lower() not in WORKER_JOB_TYPES:
            die(f"job type {skill.id} has no worker")
    if any(skill.id.lower() == "pr-review" for skill in skills):
        prompt = paths.prompts / "pr-review.md"
        if not prompt.is_file():
            die(f"prompt template {prompt} is missing")


def _cmd_dispatch(args: list[str], *, wrapper: str | None = None) -> None:
    """Start queued jobs while capacity allows.

    `wrapper` is the command string tmux runs inside each job's worktree. The
    default runs this installation's `runner work`; tests supply a harmless
    command. A wrapper is only meaningful once the worker's configuration is
    present, so the preflight runs even when a wrapper was injected.
    """
    from . import github_act, runner_status
    from .main import die, open_store

    paths = _runner_base()
    if wrapper is None:
        from .runner_worker import worker_wrapper

        try:
            wrapper = worker_wrapper(_agent_home())
        except ValueError as exc:
            # The messages are fixed texts naming the missing absolute input.
            die(f"dispatch: {exc}")
    _worker_preflight(paths)
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        runner_config = load_json(paths.runner_config, "runner config")
        max_concurrent = _max_concurrent(runner_config)
        # The announcement acts as the policy's login. Loading the policy here
        # keeps the identity check close to the effect it guards.
        policy = load_json(paths.policy, "policy")
        login = expected_login(policy)
        _ensure_state_dirs(paths)
        store = open_store()
        announced: list[str] = []
        announce_failed: list[str] = []
        try:
            own = OwnJobs(store, work_root=paths.work)

            def announce(row: dict[str, Any]) -> bool | None:
                try:
                    marker = runner_status.status_marker(
                        row.get("id"), row.get("created_at")
                    )
                    body = runner_status.announce_body(
                        row["id"], row["attempts"], marker=marker
                    )
                except (KeyError, TypeError, ValueError):
                    # Without a usable generation no activity of this job can
                    # exist, so there is nothing to post and nothing to retire.
                    announce_failed.append(str(row.get("id")))
                    return False
                # The real store, not `own`: the executor needs its lock,
                # its home and its activity rows, which the job view lacks.
                done = runner_status.announce_status(
                    store,
                    run_argv,
                    row=row,
                    body=body,
                    home=_agent_home(),
                    expected_login=login,
                    execute_activity=github_act.execute_activity,
                )
                if done is True:
                    announced.append(str(row["id"]))
                else:
                    # False (safely unposted) and None (may still be posted)
                    # are both undelivered; the dispatch decision uses the
                    # distinction, the operator sees one line for either.
                    announce_failed.append(str(row["id"]))
                return done

            # A queued job has, by definition, no live worker, so a leftover
            # exit file or transcript belongs to an earlier attempt. finalize
            # would read the old exit code as this attempt's result.
            for row in own.rows("job"):
                if row.get("state") != "queued":
                    continue
                job_id = row.get("id")
                if not isinstance(job_id, str) or not job_id.strip():
                    continue
                _remove_if_present(paths.logs / f"{job_id}.exit")
                _remove_if_present(paths.logs / f"{job_id}.transcript")
            started, skipped = dispatch.dispatch_queued(
                own,
                run_argv,
                socket=str(paths.socket),
                repos_root=str(paths.repos),
                work_root=str(paths.work),
                wrapper=wrapper,
                max_concurrent=max_concurrent,
                announce=announce,
            )
        finally:
            store.close()
    for job_id in announced:
        print(f"job.announced {job_id}")
    for job_id in announce_failed:
        # An undelivered announcement is informational; dispatch itself decides
        # whether the job may start, and the exit status stays unchanged.
        print(f"announce failed {job_id}", file=sys.stderr)
    for job_id in started:
        print(f"job.started {job_id}")
    if not started:
        print("job.started none")
    if skipped > 0:
        print(f"dispatch skipped {skipped}", file=sys.stderr)


# ----------------------------------------------------------------------- work


def _cmd_work(args: list[str]) -> None:
    """Run the one job whose worktree is the current directory.

    This is the worker body that dispatch starts inside a job; it is not an
    operator command. It takes no runner lock because it runs for the whole job
    and because dispatch starts it immediately before writing the row as running,
    so it waits for that update before validating the row. The exit file is the
    worker's result; `supervise` reads it.

    The worker's working directory is the pull-request checkout, so `PATH` is
    reduced to its absolute entries first: an empty, `.` or relative entry would
    let a commit at the checkout root name the `git` or `gh` the worker runs.
    """
    from . import github_act, lane
    from .main import die, open_store
    from .runner_worker import (
        _absolute_path_entries,
        _worker_fail,
        _worker_job_id,
        run_worker,
    )

    if args:
        die(USAGE)
    paths = _runner_base()
    sanitized_path = _absolute_path_entries(os.environ.get("PATH", ""))
    if not sanitized_path:
        job_id = _worker_job_id(paths, os.getcwd())
        if job_id is None:
            raise SystemExit(2)
        raise SystemExit(_worker_fail(paths, job_id, "worker failed"))
    os.environ["PATH"] = sanitized_path
    store = None
    try:
        store = open_store()
        code = run_worker(
            paths,
            store,
            os.getcwd(),
            home=_agent_home(),
            runner=run_argv,
            launch=lane.launch,
            execute_activity=github_act.execute_activity,
        )
    except (Exception, SystemExit):
        # A worker that cannot even open the store still owes supervise an
        # exit file; the message stays fixed so no store diagnostic leaks.
        job_id = _worker_job_id(paths, os.getcwd())
        if job_id is None:
            raise SystemExit(2)
        code = _worker_fail(paths, job_id, "worker failed")
    finally:
        if store is not None:
            store.close()
    raise SystemExit(code)


# ------------------------------------------------------------------ supervise


def _cmd_supervise(args: list[str]) -> None:
    """Finalize finished workers and reap what killed ones left behind.

    This is not `agent supervise --session`, which watches one ledger session;
    this supervises the runner's detached jobs.
    """
    from . import github_act, runner_status
    from .main import open_store

    paths = _runner_base()
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        runner_config = load_json(paths.runner_config, "runner config")
        # Supervise needs the policy only for expected_gh_user; its job settings
        # all come from runner.json.
        policy = load_json(paths.policy, "policy")
        # Same rule as ingest: finalize compares author logins
        # case-sensitively, so the login GitHub reports is the one to use.
        login = verify_gh_login(run_argv, expected_login(policy))
        _ensure_state_dirs(paths)
        now = int(time.time())
        store = open_store()
        try:
            own = OwnJobs(store, work_root=paths.work)
            finalized, finalize_skipped = finalize.finalize_running(
                own,
                run_argv,
                socket=str(paths.socket),
                repos_root=str(paths.repos),
                runner_config=runner_config,
                login=login,
                exit_code_of=lambda job_id: exit_code_of(paths, job_id),
                transcript_of=lambda job_id: transcript_of(paths, job_id),
                now_epoch=now,
                transcript_size_of=lambda job_id: transcript_size_of(paths, job_id),
            )
            removed, killed, failed, reap_skipped = reap.reap_orphans(
                own,
                run_argv,
                socket=str(paths.socket),
                repos_root=str(paths.repos),
                work_root=str(paths.work),
                work_dirs=work_dirs(paths),
                # Mutating commands are serialised, so no dispatch can be
                # half-built while this runs; a marker would protect nothing.
                marker_of=lambda _job_id: (False, None),
                now_epoch=now,
                session_prefix=dispatch.SESSION_PREFIX,
            )
            # Report what the pass just finalized as well as older failures
            # that still need their outcome comment.
            reported, report_failed = runner_status.report_outcomes(
                own,
                store,
                run_argv,
                home=_agent_home(),
                expected_login=login,
                execute_activity=github_act.execute_activity,
            )
        finally:
            store.close()
    for job_id in finalized:
        print(f"job.finalized {job_id}")
    if not finalized:
        print("job.finalized none")
    for job_id in removed:
        print(f"worktree.removed {job_id}")
    for name in killed:
        print(f"session.killed {name}")
    for job_id in reported:
        print(f"job.reported {job_id}")
    for job_id in report_failed:
        # A failed report is retried on the next pass (up to
        # MAX_REPORT_ATTEMPTS) and does not change the exit status.
        print(f"report failed {job_id}", file=sys.stderr)
    for item in failed:
        print(f"reap failed {item}", file=sys.stderr)
    total_skipped = finalize_skipped + reap_skipped
    if total_skipped > 0:
        print(f"supervise skipped {total_skipped}", file=sys.stderr)
    if failed:
        raise SystemExit(1)


# ---------------------------------------------------------------------- prune


def _cmd_prune(args: list[str]) -> None:
    from . import runner_status
    from .main import open_store

    paths = _runner_base()
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        runner_config = load_json(paths.runner_config, "runner config")
        _ensure_state_dirs(paths)
        now = int(time.time())
        pruned: list[str] = []
        store = open_store()
        try:
            own = OwnJobs(store, work_root=paths.work)
            rows = own.rows("job")
            for state in ("done", "failed"):
                days = queue.retention_days(runner_config, state)
                if days is None:
                    # Never coalesce an absent setting to 0: prunable(days=0)
                    # would delete every finished job the first time it ran.
                    print(f"prune {state} not configured")
                    continue
                for job_id in queue.prunable(
                    rows, state=state, now_epoch=now, days=days
                ):
                    row = own.row("job", job_id)
                    if row is None:
                        continue
                    # An interrupted dispatch or supervise can leave this
                    # attempt's status activity pending. Deleting the row keeps
                    # the activity, and a later scan could post it after a
                    # re-ingested job's baseline, where finalize counts it as
                    # that job's output. Retire it before anything else; a row
                    # whose comment cannot be retired is kept for the next
                    # prune, with its logs and worktree.
                    if not runner_status.retire_status_activities(
                        store, row, error="superseded by pruning the job"
                    ):
                        print(
                            f"prune skipped {job_id}: a status comment could "
                            "not be retired",
                            file=sys.stderr,
                        )
                        continue
                    # A failed job can leave its worktree behind (a failed
                    # `git worktree add`, or a cleanup `finalize` could not
                    # finish). Once the row is deleted no pass can name that
                    # directory again, so remove it first, under the same
                    # guards retry uses. A row whose worktree cannot be removed
                    # safely is kept, with its logs, for the next prune.
                    problem = remove_job_worktree(paths, job_id, row, run_argv)
                    if problem is not None:
                        print(f"prune skipped {job_id}: {problem}", file=sys.stderr)
                        continue
                    for suffix in (".exit", ".transcript", ".prompt"):
                        _remove_if_present(paths.logs / f"{job_id}{suffix}")
                    store.write(
                        "job",
                        "delete",
                        job_id,
                        {key: value for key, value in row.items() if not key.startswith("_")},
                    )
                    pruned.append(job_id)
        finally:
            store.close()
    for job_id in pruned:
        print(f"job.pruned {job_id}")
    if not pruned:
        print("job.pruned none")


# ---------------------------------------------------------------------- retry


def remove_job_worktree(
    paths: RunnerPaths,
    job_id: str,
    row: dict[str, Any],
    runner: Callable[[list[str]], Completed],
) -> str | None:
    """Remove one job's leftover worktree without ever deleting foreign data.

    Returns a problem string instead of exiting: `retry` stops before writing
    the row, and `prune` keeps a row it could not clean up so the next pass
    tries again. `None` means the path was already absent or is now gone.
    """
    path = paths.work / job_id
    try:
        bare = workspace.bare_path(str(paths.repos), row.get("repo"))
    except ValueError:
        # Without a repository there is no `git worktree prune` to run. An
        # absent path still needs no removal, so only a present one is refused.
        if not path.exists() and not os.path.islink(path):
            return None
        return f"job {job_id} has no usable repo for its worktree"
    if not path.exists() and not os.path.islink(path):
        # Git can keep administrative worktree entries after the directory is
        # gone. Prune even when there is nothing to delete, so a later add of
        # the same path does not see a stale registration.
        runner(workspace.worktree_prune_argv(bare))
        return None
    # `git worktree remove` acts on whatever the path names, so the path is
    # checked before git sees it, not only before the recursive delete below.
    # A symlink is never followed, and a path outside the root is refused:
    # either could remove data the runner does not own.
    if os.path.islink(path):
        return f"job {job_id} worktree {path} is a symlink; refusing to remove it"
    if not reap.within_root(
        os.path.realpath(str(paths.work)), os.path.realpath(str(path))
    ):
        return f"job {job_id} worktree {path} is outside the work root"
    runner(workspace.worktree_remove_argv(bare, str(path)))
    runner(workspace.worktree_prune_argv(bare))
    if not path.exists() and not os.path.islink(path):
        return None
    # Re-check after git: nothing guarantees the directory is still the one
    # inspected above. `rmtree` is allowed only for a real directory that
    # resolves strictly inside the work root.
    if os.path.islink(path):
        return f"job {job_id} worktree {path} is a symlink; refusing to remove it"
    if not reap.within_root(
        os.path.realpath(str(paths.work)), os.path.realpath(str(path))
    ):
        return f"job {job_id} worktree {path} is outside the work root"
    try:
        shutil.rmtree(path)
    except OSError as exc:
        return f"job {job_id} worktree {path}: {exc}"
    if path.exists():
        return f"job {job_id} worktree {path} could not be removed"
    # The recursive fallback left the same kind of administrative entry behind.
    runner(workspace.worktree_prune_argv(bare))
    return None


def _requeue_job(
    paths: RunnerPaths,
    store: Store,
    row: dict[str, Any],
    payload: dict[str, Any],
    runner: Callable[[list[str]], Completed],
) -> str | None:
    """Move one existing row to the queued `payload`, or say why not.

    Returns a problem text instead of exiting so `retry` and `ingest` can share
    the exact sequence. A pending status activity of the attempt being replaced
    is retired first; if that cannot be done, neither the row nor its evidence
    is touched. Only after a clean worktree removal are the previous attempt's
    artefacts removed: a stale exit code would otherwise be read as the next
    attempt's result. The single write is last, so a refused re-queue leaves
    the row unchanged.
    """
    from . import runner_status

    job_id = row.get("id")
    if not isinstance(job_id, str) or not job_id:
        return "job has no usable id"
    if not runner_status.retire_status_activities(store, row):
        return (
            f"job {job_id}: a status comment of the previous attempt "
            "could not be retired"
        )
    problem = remove_job_worktree(paths, job_id, row, runner)
    if problem is not None:
        return problem
    for suffix in (".exit", ".transcript", ".prompt"):
        _remove_if_present(paths.logs / f"{job_id}{suffix}")
    store.write("job", "update", job_id, payload)
    return None


def _cmd_retry(args: list[str]) -> None:
    from .main import die, open_store

    if len(args) != 1 or args[0].startswith("-"):
        die(USAGE)
    job_id = args[0]
    # Validate before taking the lock or opening the store: an id that cannot be
    # a path component must never reach the filesystem, and there is nothing to
    # look up under it anyway.
    if not safe_job_id(job_id):
        die(f"job id {job_id} is not a valid job id")
    paths = _runner_base()
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        _ensure_state_dirs(paths)
        store = open_store()
        try:
            row = store.row("job", job_id)
            if row is None:
                die(f"job {job_id} is missing")
            if row.get("_origin_device_id") != store.device_id():
                die(f"job {job_id} belongs to another device")
            payload = queue.retry_payload(row)
            if payload is None:
                die(
                    f"job {job_id} cannot be retried from state {row.get('state')}"
                )
            problem = _requeue_job(paths, store, row, payload, run_argv)
            if problem is not None:
                die(problem)
        finally:
            store.close()
    print(f"job.retried {job_id}")


# --------------------------------------------------------------------- status


def _cmd_status(args: list[str]) -> None:
    """Print queue counts and the running jobs. Read-only, so no lock."""
    from .main import open_store

    store = open_store()
    try:
        rows = OwnJobs(store).rows("job")
        counts = queue.state_counts(rows)
        print(
            "jobs "
            + " ".join(
                f"{state}={counts.get(state, 0)}"
                for state in ("queued", "running", "done", "failed")
            )
        )
        for row in rows:
            if row.get("state") == "running":
                print(f"job.running {row.get('id')} {row.get('started') or ''}")
    finally:
        store.close()


# --------------------------------------------------------------------- doctor


def _cmd_doctor(args: list[str]) -> None:
    """Report every configuration and environment problem. Read-only."""

    paths = _runner_base()
    problems: list[str] = []

    policy, problem = _read_json(paths.policy, "policy")
    if problem is not None:
        problems.append(problem)
    runner_config, problem = _read_json(paths.runner_config, "runner config")
    if problem is not None:
        problems.append(problem)
    catalog, problem = _read_json(paths.skills_catalog, "skills catalog")
    if problem is not None:
        problems.append(problem)
    agent_config, problem = _read_json(paths.agent_config, "agent config")
    if problem is not None:
        problems.append(problem)

    # The worker's own prerequisites: without an AI model/execution selection
    # and a prompt template, a dispatched job could only fail after dispatch
    # had already created its worktree and session.
    from .ai_accounts import AccountError, load_ai_accounts
    from .runner_worker import WORKER_JOB_TYPES

    ai = None
    try:
        ai = load_ai_accounts(paths.base.parent, defer_model_configuration=True)
    except AccountError as exc:
        problems.append(f"AI configuration is not usable: {exc}")
    if ai is not None:
        try:
            ai.require_model_configuration()
        except AccountError as exc:
            problems.append(str(exc))
        try:
            ai.require_lane_execution()
        except AccountError as exc:
            problems.append(str(exc))
    if policy is not None:
        # A policy allows queue types; the catalogue names the skill each one
        # maps to, and the skill id is what the worker is keyed by. Only a
        # readable catalogue can answer, and `health.unresolved_skills` already
        # reports one that cannot be read.
        if catalog is not None and ingest.catalog_skills(catalog) is not None:
            skills, unknown = ingest.allowed_skills(policy, catalog)
            for job_type in unknown:
                problems.append(
                    f"job type {job_type} is not in the catalogue"
                )
            for skill in skills:
                if skill.id.lower() not in WORKER_JOB_TYPES:
                    problems.append(f"job type {skill.id} has no worker")
            # The template is required when an allowed queue type resolves to
            # the one skill this worker knows how to run.
            if any(skill.id.lower() == "pr-review" for skill in skills):
                prompt = paths.prompts / "pr-review.md"
                if not prompt.is_file():
                    problems.append(f"prompt template {prompt} is missing")
            if ingest.default_skill(policy, catalog) is None:
                problems.append(
                    "policy default_skill_on_mention is not a skill in the catalogue"
                )

    # All three health checks run whenever their inputs loaded. An empty answer
    # from one does not make another unnecessary.
    if runner_config is not None:
        problems.extend(health.runner_config_problems(runner_config))
        max_concurrent = runner_config.get("max_concurrent")
        if (
            not isinstance(max_concurrent, int)
            or isinstance(max_concurrent, bool)
            or max_concurrent < 1
        ):
            problems.append("max_concurrent is not a positive integer")
    if catalog is not None and runner_config is not None:
        problems.extend(health.unresolved_skills(catalog, runner_config))
    if agent_config is not None:
        problems.extend(health.agent_config_problems(agent_config))

    expected: str | None = None
    if policy is not None:
        raw_login = policy.get("expected_gh_user")
        if isinstance(raw_login, str) and raw_login.strip():
            expected = raw_login.strip()
        else:
            problems.append("policy expected_gh_user is missing")

    for binary in ("gh", "git", "tmux", "ps"):
        if shutil.which(binary) is None:
            problems.append(f"{binary} is not on PATH")
    if not os.access(paths.base, os.W_OK):
        problems.append(f"runner directory {paths.base} is not writable")
    # Every runtime directory a command writes into, not only the top one: a
    # read-only `state/logs` would let doctor say ok while prune and retry fail.
    # A missing directory is fine, it is created on first use under a parent
    # whose own check is in this list.
    for label, directory in (
        ("state", paths.state),
        ("repos", paths.repos),
        ("work", paths.work),
        ("logs", paths.logs),
    ):
        if not directory.exists() and not directory.is_symlink():
            continue
        if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
            problems.append(f"runner {label} directory {directory} is not writable")

    if expected is not None:
        try:
            completed = run_argv(["gh", "api", "user", "--jq", ".login"])
        except OSError:
            problems.append("gh login check failed")
        else:
            actual = (completed.stdout or "").strip()
            if completed.returncode != 0:
                # gh's stderr is external text that may contain tokens or
                # configuration paths; doctor reports only that the check ran
                # and failed.
                problems.append("gh login check failed")
            elif actual.lower() != expected.lower():
                problems.append(
                    f"gh is authenticated as {actual or 'unknown'}, "
                    f"policy expects {expected}"
                )

    if problems:
        for problem in problems:
            print(f"doctor problem: {problem}")
        print(f"doctor {len(problems)} problems")
        raise SystemExit(1)
    print("doctor ok")


# ------------------------------------------------------------------ entry


def cmd_runner(args: list[str]) -> None:
    """Dispatch one `agent runner` subcommand."""
    from .main import die

    if not args:
        die(USAGE)
    sub, rest = args[0], args[1:]
    if sub == "ingest":
        _cmd_ingest(rest)
        return
    if sub == "dispatch":
        _cmd_dispatch(rest)
        return
    if sub == "work":
        _cmd_work(rest)
        return
    if sub == "supervise":
        _cmd_supervise(rest)
        return
    if sub == "prune":
        _cmd_prune(rest)
        return
    if sub == "retry":
        _cmd_retry(rest)
        return
    if sub == "status":
        _cmd_status(rest)
        return
    if sub == "doctor":
        _cmd_doctor(rest)
        return
    die(USAGE)
