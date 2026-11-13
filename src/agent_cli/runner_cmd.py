"""Operate the runner: ingest mentions, dispatch, supervise, prune, run workers.

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

`main` imports this module at the top, so the helpers from `main` are imported
inside the functions that use them — a module-level import would be circular.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
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
    "Usage: agent runner ingest --session ID|dispatch|supervise|prune|"
    "retry JOB_ID|status|doctor|work"
)

_EXIT_CODE_RE = re.compile(r"^-?[0-9]+$")


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


def verify_gh_login(runner: Callable[[list[str]], Completed], expected: str) -> None:
    """Stop unless `gh` is authenticated as `expected`, case-insensitively.

    Fail closed: a runner under the wrong identity must not ingest or supervise
    anything, because every action it takes would be attributed to that other
    account.
    """
    from .main import die

    try:
        completed = runner(["gh", "api", "user", "--jq", ".login"])
    except OSError as exc:
        die(f"cannot run gh: {exc}")
    actual = (completed.stdout or "").strip()
    if completed.returncode != 0 or actual.lower() != expected.lower():
        shown = actual or (completed.stderr or "").strip() or "unknown"
        die(f"gh is authenticated as {shown}, policy expects {expected}")


class OwnJobs:
    """A Store view restricted to this device's job rows.

    `Store.rows` returns rows from every device, and writing another device's
    row raises. A command that dispatches, finalizes, prunes or retries must
    act only on its own jobs, so the pure functions receive this view. Other
    tables pass through unchanged.
    """

    def __init__(self, store: Store) -> None:
        self._store = store
        self._device_id = store.device_id()

    def rows(self, table: str) -> list[dict[str, Any]]:
        rows = self._store.rows(table)
        if table != "job":
            return rows
        return [row for row in rows if row.get("_origin_device_id") == self._device_id]

    def row(self, table: str, row_id: str) -> dict[str, Any] | None:
        row = self._store.row(table, row_id)
        if row is None:
            return None
        if table == "job" and row.get("_origin_device_id") != self._device_id:
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


def worker_wrapper() -> str:
    """The one wrapper string every dispatched worker runs inside tmux.

    The worker derives its job from its working directory (`tmux -c
    <worktree>`), so no job id is embedded here and one string serves every
    job, which is the shape `dispatch_queued` takes.

    The interpreter that runs this installation is used rather than looking up
    `agent` on `PATH`: another program may ship a binary of that name, and it
    would then be started for every job. Running `main.py` with `-m` starts the
    same code that dispatched the job, whatever its installation is called.
    """
    from .main import home

    return shlex.join(
        [
            "env",
            f"AGENT_HOME={home()}",
            sys.executable,
            "-m",
            "agent_cli.main",
            "runner",
            "work",
        ]
    )


def exit_code_of(paths: RunnerPaths, job_id: str) -> int | None:
    """The worker's exit code, or None while there is no complete answer.

    A missing file means "no result yet". A half-written or non-numeric file is
    also "no result yet": guessing a code would record an invented outcome.
    """
    try:
        text = (paths.logs / f"{job_id}.exit").read_text(encoding="utf-8")
    except OSError:
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
    holds one does not have to reach through the other.
    """
    work = paths.work if isinstance(paths, RunnerPaths) else Path(paths)
    try:
        entries = work.iterdir()
    except OSError:
        return []
    return sorted(entry.name for entry in entries if entry.is_dir())


def _agent_settings(runner_config: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """The `agent` block and every reason it is unusable.

    The worker refuses a job when any of these rules fails, so it reports the
    reason into the job's transcript instead of starting an agent with a
    command line built from a broken setting.
    """
    agent = runner_config.get("agent")
    if not isinstance(agent, dict):
        return None, ["runner config agent is not an object"]
    problems: list[str] = []
    if not isinstance(agent.get("command"), str) or not agent["command"].strip():
        problems.append("agent.command is not a non-empty string")
    args = agent.get("args")
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        problems.append("agent.args is not a list of strings")
    if not isinstance(agent.get("prompt_flag"), str) or not agent["prompt_flag"].strip():
        problems.append("agent.prompt_flag is not a non-empty string")
    return agent, problems


# --------------------------------------------------------------------- ingest


def _cmd_ingest(args: list[str]) -> None:
    from .main import die, home, open_store, require_flag
    from .supervise import SESSION_RE

    session_id = require_flag(args, "--session")
    if SESSION_RE.match(session_id) is None:
        die("session id may contain only A-Za-z0-9_-")
    paths = runner_paths(home() / "runner")
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        _ensure_state_dirs(paths)
        policy = load_json(paths.policy, "policy")
        login = expected_login(policy)
        verify_gh_login(run_argv, login)
        store = open_store()
        try:
            # Plain Store, not OwnJobs: scan_mentions de-duplicates against every
            # device's rows. Reading a foreign row as absent would make its later
            # insert raise "already exists".
            created, skipped = ingest.scan_mentions(
                store, run_argv, session_id=session_id, login=login, policy=policy
            )
        finally:
            store.close()
    for job_id in created:
        print(f"job.queued {job_id}")
    if not created:
        print("job.queued none")
    if skipped > 0:
        print(f"ingest skipped {skipped}", file=sys.stderr)


# ------------------------------------------------------------------- dispatch


def _cmd_dispatch(args: list[str]) -> None:
    from .main import home, open_store

    paths = runner_paths(home() / "runner")
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        runner_config = load_json(paths.runner_config, "runner config")
        max_concurrent = _max_concurrent(runner_config)
        _ensure_state_dirs(paths)
        wrapper = worker_wrapper()
        store = open_store()
        try:
            own = OwnJobs(store)
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
            )
        finally:
            store.close()
    for job_id in started:
        print(f"job.started {job_id}")
    if not started:
        print("job.started none")
    if skipped > 0:
        print(f"dispatch skipped {skipped}", file=sys.stderr)


# ------------------------------------------------------------------ supervise


def _cmd_supervise(args: list[str]) -> None:
    """Finalize finished workers and reap what killed ones left behind.

    This is not `agent supervise --session`, which watches one ledger session;
    this supervises the runner's detached jobs.
    """
    from .main import home, open_store

    paths = runner_paths(home() / "runner")
    with runner_lock(paths) as acquired:
        if not acquired:
            print("runner busy")
            return
        runner_config = load_json(paths.runner_config, "runner config")
        # Supervise needs the policy only for expected_gh_user; its job settings
        # all come from runner.json.
        policy = load_json(paths.policy, "policy")
        login = expected_login(policy)
        verify_gh_login(run_argv, login)
        _ensure_state_dirs(paths)
        now = int(time.time())
        store = open_store()
        try:
            own = OwnJobs(store)
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
    for item in failed:
        print(f"reap failed {item}", file=sys.stderr)
    total_skipped = finalize_skipped + reap_skipped
    if total_skipped > 0:
        print(f"supervise skipped {total_skipped}", file=sys.stderr)
    if failed:
        raise SystemExit(1)


# ---------------------------------------------------------------------- prune


def _cmd_prune(args: list[str]) -> None:
    from .main import home, open_store

    paths = runner_paths(home() / "runner")
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
            own = OwnJobs(store)
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
                    for suffix in (".exit", ".transcript", ".prompt"):
                        _remove_if_present(paths.logs / f"{job_id}{suffix}")
                    row = own.row("job", job_id)
                    if row is None:
                        continue
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


def _remove_previous_worktree(
    paths: RunnerPaths, job_id: str, row: dict[str, Any]
) -> None:
    """Remove a retried job's leftover worktree without ever deleting foreign data."""
    from .main import die

    path = paths.work / job_id
    if not path.exists() and not os.path.islink(path):
        return
    try:
        bare = workspace.bare_path(str(paths.repos), row.get("repo"))
    except ValueError:
        die(f"job {job_id} has no usable repo for its worktree")
    run_argv(workspace.worktree_remove_argv(bare, str(path)))
    run_argv(workspace.worktree_prune_argv(bare))
    if not path.exists() and not os.path.islink(path):
        return
    # `rmtree` is allowed only for a real directory that resolves strictly
    # inside the work root. A symlink is never followed, and a path outside the
    # root is refused: either could delete data the runner does not own.
    if os.path.islink(path):
        die(f"job {job_id} worktree {path} is a symlink; refusing to remove it")
    if not reap.within_root(
        os.path.realpath(str(paths.work)), os.path.realpath(str(path))
    ):
        die(f"job {job_id} worktree {path} is outside the work root")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        die(f"job {job_id} worktree {path}: {exc}")
    if path.exists():
        die(f"job {job_id} worktree {path} could not be removed")


def _cmd_retry(args: list[str]) -> None:
    from .main import die, home, open_store

    if len(args) != 1 or args[0].startswith("-"):
        die(USAGE)
    job_id = args[0]
    paths = runner_paths(home() / "runner")
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
            # finalize reads these artefacts by job id; a stale exit code would
            # be recorded as the new attempt's result.
            for suffix in (".exit", ".transcript", ".prompt"):
                _remove_if_present(paths.logs / f"{job_id}{suffix}")
            _remove_previous_worktree(paths, job_id, row)
            store.write("job", "update", job_id, payload)
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


def _catalogue_skill_ids(catalog: dict[str, Any]) -> list[str]:
    """Non-empty skill ids the catalogue publishes.

    The catalogue is read exactly as `health.unresolved_skills` reads it: a
    list, or a mapping read by its values. Non-dict entries and ids that are
    not strings are skipped; `unresolved_skills` already reports those shapes.
    """
    raw_entries = catalog.get("skills") if isinstance(catalog, dict) else None
    if isinstance(raw_entries, list):
        entries = raw_entries
    elif isinstance(raw_entries, dict):
        entries = list(raw_entries.values())
    else:
        return []
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if isinstance(raw_id, str) and raw_id:
            ids.append(raw_id)
    return ids


def _cmd_doctor(args: list[str]) -> None:
    """Report every configuration and environment problem. Read-only."""
    from .main import home

    paths = runner_paths(home() / "runner")
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

    # All three health checks run whenever their inputs loaded. An empty answer
    # from one does not make another unnecessary.
    agent: dict[str, Any] | None = None
    agent_problems: list[str] = []
    if runner_config is not None:
        problems.extend(health.runner_config_problems(runner_config))
        max_concurrent = runner_config.get("max_concurrent")
        if (
            not isinstance(max_concurrent, int)
            or isinstance(max_concurrent, bool)
            or max_concurrent < 1
        ):
            problems.append("max_concurrent is not a positive integer")
        agent, agent_problems = _agent_settings(runner_config)
        problems.extend(agent_problems)
    if catalog is not None and runner_config is not None:
        problems.extend(health.unresolved_skills(catalog, runner_config))
    if catalog is not None:
        # A skill the catalogue publishes without its prompt template would
        # only fail when its first job runs; report it here instead.
        for skill_id in _catalogue_skill_ids(catalog):
            template = paths.prompts / f"{skill_id}.md"
            if not template.is_file():
                problems.append(f"prompt template {template} is missing")
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
    # Only a usable agent block names a command worth looking for; a broken
    # one has already been reported above.
    if agent is not None and not agent_problems:
        command = agent["command"]
        if shutil.which(command) is None:
            problems.append(f"{command} is not on PATH")
    if not os.access(paths.base, os.W_OK):
        problems.append(f"runner directory {paths.base} is not writable")
    if paths.state.exists() and not os.access(paths.state, os.W_OK):
        problems.append(f"runner state directory {paths.state} is not writable")

    if expected is not None:
        try:
            completed = run_argv(["gh", "api", "user", "--jq", ".login"])
        except OSError as exc:
            problems.append(f"gh api user failed: {exc}")
        else:
            actual = (completed.stdout or "").strip()
            if completed.returncode != 0:
                detail = (completed.stderr or "").strip() or "unknown"
                problems.append(f"gh api user failed: {detail}")
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


# ----------------------------------------------------------------------- work


def _write_exit(paths: RunnerPaths, job_id: str, code: int) -> None:
    """Write the exit code atomically: finalize must never read half a number."""
    paths.logs.mkdir(parents=True, exist_ok=True)
    tmp = paths.logs / f"{job_id}.exit.tmp"
    tmp.write_text(f"{code}\n", encoding="utf-8")
    os.replace(tmp, paths.logs / f"{job_id}.exit")


def _worker_fail(
    paths: RunnerPaths, job_id: str, message: str, *, code: int = 2
) -> int:
    """Record a worker failure and return its exit code.

    The exit file is written even when the transcript cannot be appended to:
    `finalize` must always learn that the worker ended.
    """
    print(f"agent: {message}", file=sys.stderr)
    try:
        with (paths.logs / f"{job_id}.transcript").open("ab") as transcript:
            transcript.write((message + "\n").encode("utf-8", "replace"))
    except OSError:
        pass
    try:
        _write_exit(paths, job_id, code)
    except OSError:
        pass
    return code


def _worker_job_id(paths: RunnerPaths, cwd: str) -> str | None:
    """The job whose worktree `cwd` is, or None when it is not one.

    The job id is the directory name, and the directory must be a direct child
    of the work root after resolving both. Without that rule any directory
    could name a job, and `work` would act on a row unrelated to where it runs.
    """
    work = os.path.realpath(str(paths.work))
    resolved = os.path.realpath(cwd)
    if not reap.within_root(work, resolved) or os.path.dirname(resolved) != work:
        print(f"agent: not a job worktree: {resolved}", file=sys.stderr)
        return None
    return os.path.basename(resolved)


def run_worker(
    paths: RunnerPaths,
    store: Store,
    cwd: str,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    out: Any = None,
) -> int:
    """Run one job's agent in its worktree, always leaving an exit file once identified.

    The job id is the working directory: dispatch starts the session with `-c
    <worktree>` and one wrapper string serves every job. A failure before the
    job can be identified writes nothing, because there is no job id to write
    for. Every later failure writes exit code 2 so `finalize` never waits on a
    worker that died early.
    """
    if out is None:
        out = sys.stdout.buffer
    job_id = _worker_job_id(paths, cwd)
    if job_id is None:
        return 2
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"agent: cannot create {paths.logs}: {exc}", file=sys.stderr)
        return 2

    # No state check: this process was started for this job and has to record
    # an outcome even if the row moved on while the session was being set up.
    row = store.row("job", job_id)
    if row is None:
        return _worker_fail(paths, job_id, f"job {job_id} is not in the store")
    if row.get("_origin_device_id") != store.device_id():
        return _worker_fail(
            paths, job_id, f"job {job_id} belongs to another device"
        )
    repo = row.get("repo")
    ref = row.get("ref")
    job_type = row.get("job_type")
    if (
        not isinstance(repo, str)
        or not repo.strip()
        or not isinstance(ref, str)
        or not ref.strip()
        or not isinstance(job_type, str)
        or not job_type.strip()
    ):
        return _worker_fail(
            paths, job_id, f"job {job_id} is missing repo, ref or job_type"
        )

    runner_config, problem = _read_json(paths.runner_config, "runner config")
    if problem is not None or runner_config is None:
        return _worker_fail(paths, job_id, problem or "runner config is not an object")
    agent, agent_problems = _agent_settings(runner_config)
    if agent is None or agent_problems:
        return _worker_fail(
            paths, job_id, "; ".join(agent_problems) or "agent settings are missing"
        )

    skills = runner_config.get("skills")
    skill_cfg = skills.get(job_type) if isinstance(skills, dict) else None
    deny = skill_cfg.get("deny") if isinstance(skill_cfg, dict) else None
    if deny is None:
        defaults = runner_config.get("defaults")
        deny = defaults.get("deny") if isinstance(defaults, dict) else None
    if not isinstance(deny, list) or any(not isinstance(item, str) for item in deny):
        return _worker_fail(
            paths, job_id, f"job type {job_type} has no usable deny list"
        )

    prompt_template = paths.prompts / f"{job_type}.md"
    try:
        template = prompt_template.read_text(encoding="utf-8")
    except OSError as exc:
        return _worker_fail(
            paths, job_id, f"prompt template {prompt_template}: {exc}"
        )
    prompt_text = (
        template.replace("{{REPO}}", repo)
        .replace("{{REF}}", ref)
        .replace("{{JOB_ID}}", job_id)
    )
    prompt_path = paths.logs / f"{job_id}.prompt"
    try:
        prompt_path.write_text(prompt_text, encoding="utf-8")
    except OSError as exc:
        return _worker_fail(paths, job_id, f"cannot write {prompt_path}: {exc}")

    argv = [agent["command"], *agent["args"]]
    for item in deny:
        argv.extend(["--deny", item])
    argv.extend([agent["prompt_flag"], str(prompt_path)])

    transcript_path = paths.logs / f"{job_id}.transcript"
    try:
        process = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError:
        # Anything the operating system refuses before the agent runs (missing
        # executable, no permission) is the conventional 127.
        return _worker_fail(
            paths, job_id, f"cannot start {agent['command']}", code=127
        )

    try:
        # "wb" is deliberate: a new attempt starts a fresh transcript.
        with transcript_path.open("wb") as transcript:
            stream = process.stdout
            if stream is not None:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    out.flush()
                    transcript.write(chunk)
                    # The stall watchdog reads this size; flush every chunk so
                    # a live worker is never mistaken for a quiet one.
                    transcript.flush()
    except OSError as exc:
        return _worker_fail(paths, job_id, f"worker output failed: {exc}")

    code = process.wait()
    if not isinstance(code, int) or isinstance(code, bool):
        code = 2
    _write_exit(paths, job_id, code)
    return code


# ------------------------------------------------------------------ entry


def cmd_runner(args: list[str]) -> None:
    """Dispatch one `agent runner` subcommand."""
    from .main import die, home, open_store

    if not args:
        die(USAGE)
    sub, rest = args[0], args[1:]
    if sub == "ingest":
        _cmd_ingest(rest)
        return
    if sub == "dispatch":
        _cmd_dispatch(rest)
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
    if sub == "work":
        paths = runner_paths(home() / "runner")
        cwd = os.getcwd()
        # Validate before touching the store: outside a job worktree this
        # command must leave no trace at all, and opening the store writes.
        if _worker_job_id(paths, cwd) is None:
            raise SystemExit(2)
        store = open_store()
        try:
            code = run_worker(paths, store, cwd)
        finally:
            store.close()
        raise SystemExit(code)
    die(USAGE)
