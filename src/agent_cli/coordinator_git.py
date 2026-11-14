"""Git checkout, signed commits, push, and draft publication."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .coordinator_common import (
    ORDER_KIND,
    PROTECTED_BRANCHES,
    CoordinatorError,
    DraftHeadNotVerifiedError,
    Runner,
    account_for,
    as_int,
    coord,
    gh_json,
    is_order,
    is_sha,
    pr_head_ref,
    publication_repo,
    redact,
    save_task,
    scoped,
    target_repo,
)
from .coordinator_config import RepositoryConfig, WorkerConfig
from . import github_act
from .runtime import Completed
from .store import Store


def repo_cfg(
    worker: WorkerConfig,
    repo: str,
    task: dict[str, Any] | None = None,
) -> RepositoryConfig:
    cfg = worker.repositories.get(repo)
    if cfg is not None:
        return cfg
    for key, value in worker.repositories.items():
        if key.casefold() == repo.casefold():
            return value
    if task is not None:
        source = coord(task).get("source")
        pinned = source.get("repository_config") if isinstance(source, dict) else None
        if isinstance(pinned, dict) and pinned.get("repo") == repo:
            try:
                # The commands come from the local worker config, not from the task; the
                # execution binding pins their hash, so a changed command blocks the task.
                return RepositoryConfig(
                    repo=repo,
                    base=str(pinned["base"]),
                    publication_repo=str(pinned["publication_repo"]),
                    check_argv=tuple(worker.owner_check_argv),
                    readiness_argv=tuple(worker.owner_readiness_argv or ()),
                )
            except (KeyError, TypeError):
                raise CoordinatorError(f"repository {repo} has invalid pinned configuration") from None
    raise CoordinatorError(f"repository {repo} is not configured")


def control_dir(worker: WorkerConfig, task_id: str) -> Path:
    return Path(worker.workspace_root) / ".coordinator-control" / str(task_id)


def git(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    worktree: str,
    *parts: str,
) -> Completed:
    if parts and parts[0] in {"add", "commit", "push"}:
        verify_checkout_identity(store, worker, runner, worktree)
    git_runner = scoped(store, worker.session_id, runner, require_git=True)
    return git_runner(["git", "-C", worktree, *parts])


def require_git_ok(completed: Completed, label: str) -> None:
    if completed.returncode != 0:
        raise CoordinatorError(redact(completed.stderr or completed.stdout or f"{label} failed"))


def verify_signed_clean_head(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    worktree: str,
) -> str:
    """Fail closed on failed cryptographic verification.

    SSH verification requires trusted allowed-signers in the Git account
    executor environment/config. Signature text (gpgsig / BEGIN) is never
    treated as proof of verification.
    """
    status = git(store, worker, runner, worktree, "status", "--porcelain", "--untracked-files=all")
    require_git_ok(status, "git status")
    if (status.stdout or "").strip():
        raise CoordinatorError("worktree is not clean")
    head = git(store, worker, runner, worktree, "rev-parse", "HEAD")
    require_git_ok(head, "rev-parse HEAD")
    sha = (head.stdout or "").strip().lower()
    if not is_sha(sha):
        raise CoordinatorError("cannot read HEAD")
    verify = git(store, worker, runner, worktree, "verify-commit", sha)
    if verify.returncode != 0:
        raise CoordinatorError(
            "HEAD failed cryptographic signature verification "
            "(configure trusted allowed-signers for SSH signing in the Git account executor)"
        )
    return sha


def stage_sign_commit_if_changes(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    worktree: str,
    message: str,
) -> str | None:
    status = git(store, worker, runner, worktree, "status", "--porcelain", "--untracked-files=all")
    require_git_ok(status, "git status")
    if not (status.stdout or "").strip():
        return None
    add = git(store, worker, runner, worktree, "add", "-A")
    require_git_ok(add, "git add")
    require_git_ok(git(store, worker, runner, worktree, 'diff', '--cached', '--check'), 'patch formatting')
    paths = git(store, worker, runner, worktree, 'diff', '--cached', '--name-only')
    require_git_ok(paths, 'staged file scope')
    for name in paths.stdout.splitlines():
        if (name in {'ai-accounts.json', 'github-accounts.json', 'coordinator.json', '.env'}
                or name.startswith(('.agent-coordinator/', '.coordinator-control/', '.ssh/', '.config/'))):
            raise CoordinatorError('device configuration or control files cannot be published')
    patch = git(store, worker, runner, worktree, 'diff', '--cached')
    require_git_ok(patch, 'staged patch inspection')
    if re.search(r'github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|-----BEGIN (?:(?:[A-Z]+ )*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----', patch.stdout):
        raise CoordinatorError('potential credential material in patch; publication blocked')
    # Explicit -S retains configured identity from the account runner (-c commit.gpgsign).
    commit = git(store, worker, runner, worktree, "commit", "-S", "-m", message)
    require_git_ok(commit, "signed commit")
    return verify_signed_clean_head(store, worker, runner, worktree)


START_COMMIT_MESSAGE = "Open the draft pull request."


def sign_empty_commit(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    worktree: str,
    message: str,
    *,
    base_sha: str,
) -> str:
    """Create one signed empty commit on the unchanged base head; return the verified head."""
    status = git(
        store,
        worker,
        runner,
        worktree,
        "status",
        "--porcelain",
        "--untracked-files=all",
    )
    require_git_ok(status, "git status")
    if (status.stdout or "").strip():
        raise CoordinatorError("worktree is not clean")
    head = git(store, worker, runner, worktree, "rev-parse", "HEAD")
    require_git_ok(head, "rev-parse HEAD")
    if (head.stdout or "").strip().lower() != base_sha:
        raise CoordinatorError("start commit requires an unchanged base head")
    commit = git(
        store,
        worker,
        runner,
        worktree,
        "commit",
        "-S",
        "--allow-empty",
        "-m",
        message,
    )
    require_git_ok(commit, "signed start commit")
    return verify_signed_clean_head(store, worker, runner, worktree)


def push_branch(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    worktree: str,
    branch: str,
) -> str:
    if branch in PROTECTED_BRANCHES:
        raise CoordinatorError(f"refusing to push protected branch {branch}")
    if verify_checkout_identity(store, worker, runner, worktree).get('branch') != branch:
        raise CoordinatorError('push branch differs from owned checkout')
    sha = verify_signed_clean_head(store, worker, runner, worktree)
    push = git(
        store,
        worker,
        runner,
        worktree,
        "push",
        "--",
        "publication",
        f"HEAD:refs/heads/{branch}",
    )
    require_git_ok(push, "push signed task head")
    return sha


def _marker_path(worker: WorkerConfig, task_id: str) -> Path:
    return control_dir(worker, task_id) / "checkout.json"


def _write_marker(worker: WorkerConfig, task_id: str, payload: dict[str, Any]) -> None:
    path = _marker_path(worker, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _read_marker(worker: WorkerConfig, task_id: str) -> dict[str, Any] | None:
    path = _marker_path(worker, task_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _remote_url(store: Store, worker: WorkerConfig, runner: Runner, worktree: str, name: str) -> str:
    got = git(store, worker, runner, worktree, "remote", "get-url", name)
    if got.returncode != 0:
        raise CoordinatorError(f"missing git remote {name}")
    return (got.stdout or "").strip()


def _expected_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def _checkout_path(worker: WorkerConfig, worktree: str) -> Path:
    path = Path(worktree)
    if (path.parent != worker.workspace_root or path.resolve() != path
            or path.name in {"", ".", "..", ".coordinator-control"}
            or path.is_symlink()):
        raise CoordinatorError("checkout path does not belong to configured workspace")
    return path


def verify_checkout_identity(store: Store, worker: WorkerConfig, runner: Runner, worktree: str) -> dict[str, Any]:
    """Validate prior script ownership, configured route and current feature branch."""
    path = _checkout_path(worker, worktree)
    marker = _read_marker(worker, path.name)
    if not marker or marker.get("phase") != "ready":
        raise CoordinatorError("checkout lacks completed script ownership marker")
    if marker.get("task_id") != path.name or marker.get("session_id") != worker.session_id:
        raise CoordinatorError("checkout ownership mismatch")
    task = store.row('task', path.name)
    if (task is None or task.get('session_id') != worker.session_id
            or task.get('_origin_device_id') != store.device_id()):
        raise CoordinatorError('checkout task is missing or foreign')
    checkpoint = coord(task)
    for key in ('base_sha', 'branch', 'worktree'):
        if checkpoint.get(key) is not None and checkpoint[key] != marker.get(key):
            raise CoordinatorError('checkout task checkpoint changed')
    cfg = repo_cfg(worker, str(marker.get("target_repo") or ""), task)
    account = account_for(store, worker.session_id)
    if (marker.get("base") != cfg.base or marker.get("publication_repo") != cfg.publication_repo
            or marker.get("worker_login") != account.login.casefold()):
        raise CoordinatorError("checkout configuration changed; refusing to repoint")
    if not (path / ".git").is_dir() or (path / ".git").is_symlink():
        raise CoordinatorError("checkout git directory is missing or replaced")
    raw = scoped(store, worker.session_id, runner, require_git=True)
    def read(*args):
        result = raw(["git", "-C", worktree, *args])
        require_git_ok(result, "checkout identity")
        return result.stdout.strip()
    if read("remote", "get-url", "origin") != _expected_url(cfg.repo):
        raise CoordinatorError("checkout target remote changed")
    if read("remote", "get-url", "publication") != _expected_url(cfg.publication_repo):
        raise CoordinatorError("checkout publication remote changed")
    branch = str(marker.get("branch") or "")
    if not branch or branch in PROTECTED_BRANCHES or read("rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise CoordinatorError("checkout feature branch changed")
    base_sha = str(marker.get("base_sha") or "")
    if not is_sha(base_sha):
        raise CoordinatorError("checkout base is not pinned")
    require_git_ok(raw(["git", "-C", worktree, "merge-base", "--is-ancestor", base_sha, "HEAD"]), "pinned base ancestry")
    return marker


def adopt_published_head(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    expected_old: str,
    expected_new: str | None,
    base_sha: str | None,
) -> str | None:
    """Bring the owned checkout to the head a script-owned rebase published.

    Returns the adopted head, or None when the published head is still
    ``expected_old``. It changes no field of the task.
    """
    c = coord(task)
    worktree = str(c.get("worktree") or "")
    branch = str(c.get("branch") or "")
    if not worktree or not branch:
        raise CoordinatorError("checkout is missing")
    verify_checkout_identity(store, worker, runner, worktree)
    local = verify_signed_clean_head(store, worker, runner, worktree)
    fetched = git(
        store,
        worker,
        runner,
        worktree,
        "fetch",
        "--no-tags",
        "publication",
        f"+refs/heads/{branch}:refs/remotes/publication/{branch}",
    )
    require_git_ok(fetched, "fetch published head")
    read_remote = git(
        store,
        worker,
        runner,
        worktree,
        "rev-parse",
        f"refs/remotes/publication/{branch}",
    )
    require_git_ok(read_remote, "read published head")
    remote = (read_remote.stdout or "").strip().lower()
    if not is_sha(remote):
        raise CoordinatorError("cannot read the published head")
    if remote == expected_old:
        if local != expected_old:
            raise CoordinatorError("checkout head differs from the published head")
        return None
    if expected_new is None or remote != expected_new:
        raise CoordinatorError(
            "published head changed outside the recorded conflict resolution"
        )
    if local not in (expected_old, expected_new):
        raise CoordinatorError(
            "checkout head differs from the head the conflict resolution replaced"
        )
    if is_sha(str(base_sha or "")):
        require_git_ok(
            git(
                store,
                worker,
                runner,
                worktree,
                "merge-base",
                "--is-ancestor",
                str(base_sha),
                remote,
            ),
            "rebased base ancestry",
        )
    if local != remote:
        require_git_ok(
            git(store, worker, runner, worktree, "reset", "--hard", remote),
            "adopt rebased head",
        )
    head = verify_signed_clean_head(store, worker, runner, worktree)
    if head != remote:
        raise CoordinatorError("checkout did not reach the published head")
    verify_checkout_identity(store, worker, runner, worktree)
    return head


def phase_checkout(store: Store, worker: WorkerConfig, task: dict[str, Any], runner: Runner) -> list[str]:
    c = coord(task)
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]), task)
    publication = str(source.get("publication_repo") or cfg.publication_repo)
    if publication != cfg.publication_repo or source.get("base", cfg.base) != cfg.base:
        raise CoordinatorError("checkout route changed after assignment")
    branch = str(c.get("branch") or f"task-{str(task['id'])[:8]}")
    if branch in PROTECTED_BRANCHES or not branch.startswith("task-"):
        raise CoordinatorError("refusing unexpected task feature branch")
    worktree = _checkout_path(worker, str(worker.workspace_root / str(task["id"])))
    prior = _read_marker(worker, str(task["id"]))
    # Crucially, existing directories cannot create their own proof of ownership.
    if worktree.exists() and prior is None:
        raise CoordinatorError("existing checkout lacks prior ownership marker")
    intent = {
        "task_id": task["id"], "session_id": worker.session_id,
        "target_repo": cfg.repo, "publication_repo": publication,
        "branch": branch, "base": cfg.base, "worktree": str(worktree),
        "worker_login": account_for(store, worker.session_id).login.casefold(),
    }
    if prior is not None:
        if any(prior.get(key) != value for key, value in intent.items()):
            raise CoordinatorError("existing checkout ownership or configuration changed")
        marker = prior
    else:
        marker = {**intent, "phase": "reserved"}
        _write_marker(worker, task["id"], marker)
    c["checkout_intent"] = intent
    save_task(store, task)
    raw = scoped(store, worker.session_id, runner, require_git=True)
    def run(*args):
        result = raw(["git", "-C", str(worktree), *args])
        require_git_ok(result, "checkout command")
        return result.stdout.strip()
    if not worktree.exists():
        if marker["phase"] != "reserved":
            raise CoordinatorError("owned checkout disappeared; refusing recreation")
        worker.workspace_root.mkdir(parents=True, exist_ok=True)
        result = raw(["git", "clone", "--no-checkout", "--single-branch", "--branch", cfg.base,
                      "--", _expected_url(cfg.repo), str(worktree)])
        require_git_ok(result, "clone target repository")
    if not (worktree / ".git").is_dir() or (worktree / ".git").is_symlink():
        raise CoordinatorError("interrupted checkout needs recovery; existing files retained")
    if run("remote", "get-url", "origin") != _expected_url(cfg.repo):
        raise CoordinatorError("existing checkout origin mismatch")
    remotes = run("remote").splitlines()
    if "publication" not in remotes:
        if marker["phase"] != "reserved":
            raise CoordinatorError("owned publication remote disappeared")
        run("remote", "add", "publication", _expected_url(publication))
    if run("remote", "get-url", "publication") != _expected_url(publication):
        raise CoordinatorError("existing checkout publication remote mismatch")
    if not marker.get("base_sha"):
        run("fetch", "--", "origin", f"refs/heads/{cfg.base}:refs/remotes/origin/{cfg.base}")
        base_sha = run("rev-parse", f"refs/remotes/origin/{cfg.base}")
        if not is_sha(base_sha):
            raise CoordinatorError("cannot pin target base")
        marker.update(base_sha=base_sha, phase="base-pinned")
        _write_marker(worker, task["id"], marker)
    base_sha = str(marker["base_sha"])
    if not is_sha(base_sha):
        raise CoordinatorError("invalid pinned base")
    current = run("rev-parse", "--abbrev-ref", "HEAD")
    if marker["phase"] == "base-pinned":
        if current != branch:
            # -b refuses an existing feature ref; never reset an existing branch.
            run("checkout", "-b", branch, base_sha)
        elif run("rev-parse", "HEAD") != base_sha:
            raise CoordinatorError("unexpected commits before checkout initialization completed")
        if run("status", "--porcelain", "--untracked-files=all"):
            raise CoordinatorError("refusing dirty checkout initialization")
        marker["phase"] = "ready"
        _write_marker(worker, task["id"], marker)
    verify_checkout_identity(store, worker, runner, str(worktree))
    if run("status", "--porcelain", "--untracked-files=all"):
        raise CoordinatorError("refusing dirty checkout recovery")
    c.update(
        worktree=str(worktree),
        base_sha=base_sha,
        branch=branch,
        publication_repo=publication,
        head_sha=run("rev-parse", "HEAD"),
        phase="start_draft" if is_order(task) or worker.draft_on_accept else "implement",
    )
    save_task(store, task)
    return [f"checkout ready on pinned target base {base_sha[:7]}"]


def queue_activity(
    store: Store,
    *,
    activity_id: str,
    session_id: str,
    typ: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    existing = store.row("activity", activity_id)
    row = {
        "id": activity_id,
        "session_id": session_id,
        "type": typ,
        "payload": payload,
        "execution_status": "pending",
    }
    if existing is not None:
        if existing.get("execution_status") == "done":
            return existing
        if existing.get("execution_status") == "error":
            existing_payload = existing.get("payload")
            if (
                isinstance(existing_payload, dict)
                and existing_payload.get("exact_body") is True
            ):
                if (
                    existing.get("session_id") != session_id
                    or existing.get("type") != typ
                    or existing_payload != payload
                ):
                    raise CoordinatorError("refusing to replace an existing exact-body activity")
                row["payload"] = existing_payload
            store.write("activity", "update", activity_id, row)
            return row
        return existing
    store.write_with_advisory(
        "activity",
        "insert",
        activity_id,
        row,
        lock_key=f"coordinator-activity:{activity_id}",
        skip=lambda: store.row("activity", activity_id) is not None,
    )
    return store.row("activity", activity_id) or row


def execute_github(store: Store, runner: Runner, *, activity_ids: tuple[str, ...]) -> list[str]:
    """Execute only the concrete script intents named by this step."""
    selected = frozenset(activity_ids)
    if not selected:
        return []
    for aid in selected:
        row = store.row('activity', aid)
        if row is None or row.get('_origin_device_id') != store.device_id():
            raise CoordinatorError('GitHub intent is missing or foreign')
    bound_report_tasks: dict[str, set[str]] = {}
    for aid in selected:
        row = store.row("activity", aid)
        payload = row.get("payload") if isinstance(row, dict) else None
        binding = (
            payload.get(github_act.A38_REPORT_BINDING_KEY)
            if isinstance(payload, dict)
            else None
        )
        task_id = binding.get("task_id") if isinstance(binding, dict) else None
        if isinstance(task_id, str) and task_id:
            bound_report_tasks.setdefault(task_id, set()).add(aid)
    class Scoped:
        def __getattr__(self, name):
            return getattr(store, name)
        def pending_work(self):
            return [row for row in store.pending_work() if row.get('id') in selected]
        def rows(self, table):
            rows = store.rows(table)
            return [row for row in rows if row.get('id') in selected] if table == 'activity' else rows
        def write(self, table, operation, row_id, data, **kwargs):
            if table == 'activity' and row_id in selected:
                return store.write(table, operation, row_id, data, **kwargs)
            if table == "task" and operation == "update":
                activity_ids = bound_report_tasks.get(row_id, set())
                current = store.row("task", row_id)
                if (
                    activity_ids
                    and isinstance(current, dict)
                    and current.get("_origin_device_id") == store.device_id()
                ):
                    expected = {
                        key: value
                        for key, value in deepcopy(current).items()
                        if not key.startswith("_")
                    }
                    payload = expected.get("payload")
                    coordinator = (
                        payload.get("coordinator")
                        if isinstance(payload, dict)
                        else None
                    )
                    a38 = (
                        coordinator.get("a38")
                        if isinstance(coordinator, dict)
                        else None
                    )
                    intent = (
                        a38.get("report_intent")
                        if isinstance(a38, dict)
                        else None
                    )
                    if (
                        isinstance(intent, dict)
                        and intent.get("activity_id") in activity_ids
                    ):
                        a38.pop("report_intent", None)
                        if data == expected:
                            return store.write(
                                table, operation, row_id, data, **kwargs
                            )
            raise CoordinatorError('GitHub executor attempted an unrelated write')
    return github_act.scan_github(Scoped(), runner)


def draft_pr_body(
    source: dict[str, Any],
    reused: list[str] | None = None,
    added: list[str] | None = None,
) -> str:
    """Draft text. Empty headings stay until the script has a real list to post."""
    if reused or added:
        reused_text = "\n".join(f"- {line}" for line in reused or [])
        added_text = "\n".join(f"- {line}" for line in added or [])
        details = f"Reused:\n{reused_text}\nAdded:\n{added_text}\n\n"
    else:
        details = "Reused:\nAdded:\n\n"
    if source.get("kind") == ORDER_KIND:
        short = str(source.get("order_id") or "")[:8]
        title = redact(str(source.get("title") or ""), limit=200).rstrip(". ")
        visible = (
            f"EN:\nDraft for order {short}: {title}.\n\n"
            f"DE:\nEntwurf für Auftrag {short}: {title}.\n\n"
        )
    else:
        visible = (
            f"EN:\nDraft for {source['repo']}#{source['number']}.\n\n"
            f"DE:\nEntwurf für {source['repo']}#{source['number']}.\n\n"
        )
    return visible + (
        "<details>\n<summary>Details</summary>\n\n"
        f"{details}"
        "</details>\n"
    )


def refresh_draft_body(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Post the implementer's reused and added list. The script calls GitHub, not the model."""
    c = coord(task)
    reused = task.get("reused_lines")
    added = task.get("added_lines")
    if not isinstance(reused, list) or not isinstance(added, list) or not reused or not added:
        return []
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    if not isinstance(source.get("repo"), str) or (
        source.get("number") is None and source.get("kind") != ORDER_KIND
    ):
        return []
    number = as_int(c.get("pr_number") or task.get("ref"))
    target = target_repo(task)
    if number is None or target == "":
        raise CoordinatorError("reused and added list has no draft to update")
    body = draft_pr_body(source, [str(line) for line in reused], [str(line) for line in added])
    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-pr-body:{task['id']}:{body}"))
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="pr.body",
        payload={"repo": target, "number": number, "body": body},
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        raise CoordinatorError("pr.body did not update the draft")
    return [f"posted reused and added list on {target}#{number}"]


def ensure_draft(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Push every new signed head; open draft once on first commit.

    Target repository is source.repo for PR API. publication_repo is push only.
    """
    c = coord(task)
    worktree = str(c.get("worktree") or "")
    branch = str(c.get("branch") or "")
    source = c["source"]
    cfg = repo_cfg(worker, str(source["repo"]), task)
    target = target_repo(task)
    publication = publication_repo(task, cfg)
    base_sha = str(c.get("base_sha") or "")
    if not worktree or not branch:
        return []
    verify_checkout_identity(store, worker, runner, worktree)
    log = git(store, worker, runner, worktree, "log", "--oneline", f"{base_sha}..HEAD")
    if log.returncode != 0 or not (log.stdout or "").strip():
        return []
    existing_pr = as_int(c.get("pr_number") or task.get("ref"))
    if existing_pr is not None:
        before = _verify_draft(store, worker, runner, target, existing_pr, cfg, branch, allow_ready=worker.a38)
        local_head = verify_signed_clean_head(store, worker, runner, worktree)
        if before['head']['sha'] not in {c.get('published_head'), local_head}:
            raise CoordinatorError('PR head changed outside this task; refusing to overwrite it')
    sha = push_branch(store, worker, runner, worktree, branch)
    c["head_sha"] = sha
    c['pending_published_head'] = sha
    save_task(store, task)

    if existing_pr is not None:
        after = _verify_draft(store, worker, runner, target, existing_pr, cfg, branch, allow_ready=worker.a38)
        if after['head']['sha'] != sha:
            raise DraftHeadNotVerifiedError('updated Draft head is not yet verified')
        c['published_head'] = sha
        c.pop('pending_published_head', None)
        save_task(store, task)
        return [f"pushed head {sha[:7]} to existing PR {target}#{existing_pr}"]

    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-pr-open:{task['id']}:{target}:{branch}"))
    title = str(task.get("title") or branch)
    body = draft_pr_body(source)
    head = pr_head_ref(branch, target, publication)
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="pr.open",
        payload={
            "repo": target,
            "title": title,
            "head": head,
            "base": cfg.base,
            "body": body,
        },
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        c["phase"] = "publish_draft"
        c["pr_open_activity_id"] = activity_id
        # Do not advance past publication.
        save_task(store, task)
        err = redact(str((row or {}).get("execution_error") or "pr.open pending"))
        return [f"draft pending after push head={sha[:7]}: {err}"]
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    number = as_int(result.get("number"))
    if number is None or number <= 0:
        raise CoordinatorError("pr.open done without number")
    opened = _verify_draft(store, worker, runner, target, number, cfg, branch)
    if opened['head']['sha'] != sha:
        raise CoordinatorError('Draft head does not match published task commit')
    c['published_head'] = sha
    c.pop('pending_published_head', None)
    c["pr_number"] = number
    c["pr_open_activity_id"] = activity_id
    task["ref"] = str(number)  # PR number only — never the issue number
    task["repo"] = target
    save_task(store, task)
    return [f"draft opened {target}#{number} head={sha[:7]}"]


def _verify_draft(store, worker, runner, target, number, cfg, branch, *, allow_ready=False):
    result = gh_json(scoped(store, worker.session_id, runner),
                     ['gh', 'api', f'repos/{target}/pulls/{number}'])
    if not isinstance(result, dict):
        raise CoordinatorError('Draft lookup did not return a pull request')
    base = result.get('base') or {}
    head = result.get('head') or {}
    user = result.get('user') or {}
    # An A38 guard may set Ready before a blocking second review arrives; the fix is
    # pushed to that open pull request and the guard decides Ready again for the new head.
    draft_ok = result.get('draft') is True or (allow_ready and result.get('draft') is False)
    if (result.get('state') != 'open' or not draft_ok
            or str(user.get('login', '')).casefold() != account_for(store, worker.session_id).login.casefold()
            or base.get('ref') != cfg.base
            or str((base.get('repo') or {}).get('full_name', '')).casefold() != target.casefold()
            or head.get('ref') != branch
            or str((head.get('repo') or {}).get('full_name', '')).casefold() != cfg.publication_repo.casefold()
            or not is_sha(str(head.get('sha') or ''))):
        raise CoordinatorError('Draft identity, route, head or state changed')
    return result


def phase_publish_draft(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    lines = ensure_draft(store, worker, task, runner)
    c = coord(task)
    if c.get("pr_number"):
        lines = lines + refresh_draft_body(store, worker, task, runner)
        resume = c.get("resume_phase")
        if isinstance(resume, str) and resume:
            c["phase"] = resume
            c.pop("resume_phase", None)
        else:
            c["phase"] = "inner_review" if task.get("state") == "reviewing" else "implement"
        save_task(store, task)
    return lines or ["draft reconcile idle"]


def phase_start_draft(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Open the draft pull request on an empty start commit before the first lane."""
    c = coord(task)
    worktree = str(c.get("worktree") or "")
    base_sha = str(c.get("base_sha") or "")
    lines: list[str] = []
    if as_int(c.get("pr_number")) is None:
        log = git(
            store,
            worker,
            runner,
            worktree,
            "log",
            "--oneline",
            f"{base_sha}..HEAD",
        )
        require_git_ok(log, "start draft log")
        ahead = bool((log.stdout or "").strip())
        if not ahead:
            c["start_commit_pending"] = True
            save_task(store, task)
            sha = sign_empty_commit(
                store,
                worker,
                runner,
                worktree,
                START_COMMIT_MESSAGE,
                base_sha=base_sha,
            )
            c["start_commit"] = sha
            c["head_sha"] = sha
            c.pop("start_commit_pending", None)
            save_task(store, task)
        elif c.get("start_commit_pending") is True:
            sha = verify_signed_clean_head(store, worker, runner, worktree)
            c["start_commit"] = sha
            c["head_sha"] = sha
            c.pop("start_commit_pending", None)
            save_task(store, task)
        lines = ensure_draft(store, worker, task, runner)
        c = coord(task)
        if as_int(c.get("pr_number")) is None:
            c["phase"] = "start_draft"
            save_task(store, task)
            return lines or ["start draft pending"]
    c["phase"] = "implement"
    save_task(store, task)
    return [
        f"draft opened on start commit {str(c.get('start_commit') or '')[:7]}",
        *lines,
    ]


def close_pull_request(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
    *,
    activity_id: str,
    body: str,
) -> str:
    """Comment on the task's pull request and close it; returns "CLOSED" or "MERGED"."""
    c = coord(task)
    number = as_int(c.get("pr_number") or task.get("ref"))
    target = target_repo(task)
    if number is None or not target:
        raise CoordinatorError("no pull request to close")
    api = scoped(store, worker.session_id, runner)

    def state() -> str:
        view = gh_json(
            api,
            ["gh", "pr", "view", str(number), "--repo", target, "--json", "state"],
        )
        return str(view.get("state") or "").upper() if isinstance(view, dict) else ""

    current = state()
    if current == "MERGED":
        return "MERGED"
    if current == "OPEN":
        queue_activity(
            store,
            activity_id=activity_id,
            session_id=worker.session_id,
            typ="comment.post",
            payload={"repo": target, "number": number, "body": body, "target": "pr"},
        )
        execute_github(store, runner, activity_ids=(activity_id,))
        row = store.row("activity", activity_id)
        if row is None or row.get("execution_status") != "done":
            raise CoordinatorError("close comment was not verified published")
        closed = api(["gh", "pr", "close", str(number), "--repo", target])
        if closed.returncode != 0:
            raise CoordinatorError(
                redact(closed.stderr or closed.stdout or "gh pr close failed")
            )
        current = state()
    if current != "CLOSED":
        raise CoordinatorError("pull request is not closed")
    return "CLOSED"
