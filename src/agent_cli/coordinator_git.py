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
        if (name in {'ai-accounts.json', 'ai-subscriptions.json', 'github-accounts.json', 'coordinator.json', '.env'}
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
        def __init__(self) -> None:
            self._github_rate_limits: list[Any] = []

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
    scoped = Scoped()
    lines = github_act.scan_github(scoped, runner)
    # The device poll skips a limited account and returns. This step still
    # raises so the coordinator keeps the task phase.
    if scoped._github_rate_limits:
        raise scoped._github_rate_limits[0]
    return lines


def draft_pr_body(
    source: dict[str, Any],
    reused: list[str] | None = None,
    added: list[str] | None = None,
    summary_de: str | None = None,
) -> str:
    """Draft text. The reused and added list and its headings appear only when the script posts a list.

    An order's DE block is German. With a change summary that is not the
    English title, the full stored sentence follows the order label. Without
    one, or when the stored sentence is only that title, this text names the
    order and leaves the English title in the EN block. A refresh posts that
    order-only line over a published English title only while it also posts
    the reused and added list. It does not post that line over a German
    sentence, and it does not post it when the list is absent.
    """
    details = ""
    if reused or added:
        reused_text = "\n".join(f"- {line}" for line in reused or [])
        added_text = "\n".join(f"- {line}" for line in added or [])
        details = f"Reused:\n{reused_text}\nAdded:\n{added_text}\n\n"
    if source.get("kind") == ORDER_KIND:
        short = str(source.get("order_id") or "")[:8]
        title = redact(str(source.get("title") or ""), limit=200).rstrip(". ")
        if summary_de is None:
            de_line = f"Entwurf für Auftrag {short}."
        else:
            if (
                not isinstance(summary_de, str)
                or "\n" in summary_de
                or not summary_de.strip()
            ):
                raise CoordinatorError("task DE summary is missing")
            full = redact(summary_de.strip(), limit=800).strip()
            core = full.rstrip(". ")
            # The English title is not German content. Any other summary is
            # the full sentence and is published whole, including its period.
            if not core or core.casefold() == title.casefold():
                de_line = f"Entwurf für Auftrag {short}."
            else:
                sentence = full if full.endswith(".") else f"{core}."
                de_line = f"Entwurf für Auftrag {short}: {sentence}"
        visible = (
            f"EN:\nDraft for order {short}: {title}.\n\n"
            f"DE:\n{de_line}\n\n"
        )
    else:
        visible = (
            f"EN:\nDraft for {source['repo']}#{source['number']}.\n\n"
            f"DE:\nEntwurf für {source['repo']}#{source['number']}.\n\n"
        )
    if not details:
        return visible
    return visible + (
        "<details>\n<summary>Details</summary>\n\n"
        f"{details}"
        "</details>\n"
    )


def _description_labeled_line(body: str, label: str) -> str | None:
    """First labeled line of a published description.

    Accepts a block whose text is on the next line and a label whose text
    is on the same line. Carriage returns do not hide that line.
    """
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")
    marker = f"{label}\n"
    if marker in normalized:
        return normalized.split(marker, 1)[1].split("\n", 1)[0]
    for line in normalized.split("\n"):
        if line.startswith(label):
            text = line[len(label) :].strip()
            if text:
                return text
    return None


def _description_de_line(body: str) -> str | None:
    """First DE line of a published description."""
    return _description_labeled_line(body, "DE:")


def _description_en_line(body: str) -> str | None:
    """First EN line of a published description."""
    return _description_labeled_line(body, "EN:")


def _line_sentence(line: str | None) -> str:
    if not isinstance(line, str) or ": " not in line:
        return ""
    return line.split(": ", 1)[1].strip().rstrip(". ")


def _de_repeats_english_title(body: str | None) -> bool:
    """True when the DE sentence is the English title from the EN line."""
    if not isinstance(body, str) or not body:
        return False
    en_sentence = _line_sentence(_description_en_line(body)).casefold()
    de_sentence = _line_sentence(_description_de_line(body)).casefold()
    return bool(en_sentence) and en_sentence == de_sentence


def _order_de_has_sentence(source: dict[str, Any], body: str) -> bool:
    if source.get("kind") != ORDER_KIND:
        return False
    short = str(source.get("order_id") or "")[:8]
    line = _description_de_line(body)
    return line is not None and line != f"Entwurf für Auftrag {short}."


def _english_title_keys(task: dict[str, Any]) -> set[str]:
    """Casefolded order titles a DE line must not repeat.

    The source title and the task title are both titles. A session prefix
    before `` - `` is not part of the order title.
    """
    keys: set[str] = set()
    source = coord(task).get("source")
    source = source if isinstance(source, dict) else {}
    for raw in (source.get("title"), task.get("title")):
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = redact(raw, limit=200).strip()
        folded = text.rstrip(". ").casefold()
        if folded:
            keys.add(folded)
        if " - " in text:
            tail = text.split(" - ", 1)[1].strip().rstrip(". ").casefold()
            if tail:
                keys.add(tail)
    return keys


def _repeats_english_title(task: dict[str, Any], summary: str) -> bool:
    text = redact(summary.strip(), limit=800).rstrip(". ")
    return not text or text.casefold() in _english_title_keys(task)


def _pending_implementer_summary(task: dict[str, Any]) -> str | None:
    """German sentence from an implementer checkpoint.

    The draft refresh runs before the round commit saves that sentence, and
    stores it itself. The checkpoint is the full sentence for this round. A
    stored summary that only repeats the English title does not hide it.
    """
    outcome = coord(task).get("lane_outcome")
    if not isinstance(outcome, dict) or outcome.get("applied") is True:
        return None
    if outcome.get("role") != "implementer":
        return None
    if outcome.get("result") not in {"done", "no-change"}:
        return None
    value = outcome.get("summary_de")
    if not isinstance(value, str) or not value.strip() or "\n" in value:
        return None
    return value.strip()


def _stored_de_summary(task: dict[str, Any], *, has_lists: bool) -> str | None:
    """Full German sentence for the draft, never the English title.

    The open implementer checkpoint is that sentence when it is not the
    English title. It replaces a stored summary that is missing, older, or
    only the title. The value returned here is the full sentence the refresh
    stores and publishes.
    """
    raw = task.get("change_summary_de")
    pending = _pending_implementer_summary(task)
    if pending is not None and not _repeats_english_title(task, pending):
        return pending
    stored = raw if isinstance(raw, str) and raw.strip() and "\n" not in raw else None
    if stored is not None and _repeats_english_title(task, stored):
        return None
    if stored is None:
        if has_lists and raw is not None:
            from .coordinator_a38 import _summary

            return _summary(task, "de")
        return None
    from .coordinator_a38 import _summary

    return _summary(task, "de")


def _order_de_sentence(task: dict[str, Any]) -> str | None:
    """Full German order sentence, or None when that sentence is not stored.

    The order-only line and the English title are not this sentence. Callers
    that publish a draft use None to mean the sentence is not stored. Posting
    the reused and added list may then replace a published English title with
    the order-only line. It does not replace a German sentence.
    """
    source = coord(task).get("source")
    source = source if isinstance(source, dict) else {}
    if source.get("kind") != ORDER_KIND:
        return None
    reused = task.get("reused_lines")
    added = task.get("added_lines")
    has_lists = (
        isinstance(reused, list)
        and isinstance(added, list)
        and bool(reused)
        and bool(added)
    )
    summary = _stored_de_summary(task, has_lists=has_lists)
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or "\n" in summary
        or _repeats_english_title(task, summary)
    ):
        return None
    return summary.strip()


def _published_de_error() -> CoordinatorError:
    return CoordinatorError(
        "published description DE line is not the stored German sentence"
    )


def _stored_sentence_line(source: dict[str, Any], sentence: str) -> str | None:
    """DE line for a full German sentence, not the order-only fallback."""
    line = _description_de_line(draft_pr_body(source, summary_de=sentence))
    short = str(source.get("order_id") or "")[:8]
    if line is None or line == f"Entwurf für Auftrag {short}.":
        return None
    return line


def _published_de_is_english_title(
    task: dict[str, Any], source: dict[str, Any], live: str | None
) -> bool:
    """The read DE line repeats an English order title."""
    if _de_repeats_english_title(live):
        return True
    sentence = _line_sentence(_description_de_line(live or ""))
    if not sentence:
        return False
    if _repeats_english_title(task, sentence):
        return True
    title = redact(str(source.get("title") or ""), limit=200).strip().rstrip(". ")
    return bool(title) and sentence.casefold() == title.casefold()


def _require_stored_sentence(
    source: dict[str, Any],
    live_body: str | None,
    sentence: str | None,
    task: dict[str, Any],
) -> None:
    """The read description has to show the stored German sentence.

    A missing sentence does not make an English title acceptable. The
    order-only line is the accepted published line for that missing
    sentence, and this check returns. Any other order line still fails.
    The read line is the published description, not the body that was sent.
    """
    if _published_de_is_english_title(task, source, live_body):
        raise _published_de_error()
    if sentence is None:
        if source.get("kind") == ORDER_KIND:
            short = str(source.get("order_id") or "")[:8]
            if _description_de_line(live_body or "") == (
                f"Entwurf für Auftrag {short}."
            ):
                return
            raise _published_de_error()
        return
    line = _stored_sentence_line(source, sentence)
    if line is None or _description_de_line(live_body or "") != line:
        raise _published_de_error()


def _published_pr_body(
    store: Store,
    worker: WorkerConfig,
    runner: Runner,
    target: str,
    number: int,
) -> str | None:
    """Read the published pull request body."""
    viewed = gh_json(
        scoped(store, worker.session_id, runner),
        ["gh", "pr", "view", str(number), "--repo", target, "--json", "body"],
    )
    if not isinstance(viewed, dict):
        raise _published_de_error()
    if "body" not in viewed or viewed.get("body") is None:
        return None
    raw = viewed.get("body")
    if not isinstance(raw, str):
        raise _published_de_error()
    return raw


def _published_description_matches(live: str | None, wanted: str) -> bool:
    if live is None or _de_repeats_english_title(live):
        return False
    wanted_de = _description_de_line(wanted)
    if wanted_de is None or _description_de_line(live) != wanted_de:
        return False
    if _de_repeats_english_title(wanted):
        return False
    if "\nReused:\n" in wanted and "\nReused:\n" not in live:
        return False
    return True


def _description_was_checked(row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    result = row.get("result")
    return isinstance(result, dict) and result.get("description_checked") is True


def _pr_body_attempts(
    store: Store, *, body: str, number: int, target: str
) -> int:
    """Checked updates and failed checks for this description.

    A done ``pr.body`` row from ``gh pr edit`` has no description check. It
    does not block the pulls update that reads the published body back.
    """
    count = 0
    for row in store.rows("activity"):
        if row.get("type") != "pr.body":
            continue
        status = row.get("execution_status")
        if status not in {"done", "error"}:
            continue
        payload = row.get("payload")
        if not (
            isinstance(payload, dict)
            and payload.get("description_check") is True
            and payload.get("body") == body
            and payload.get("number") == number
            and payload.get("repo") == target
        ):
            continue
        if status == "done" and not _description_was_checked(row):
            continue
        count += 1
    return count


def refresh_draft_body(
    store: Store,
    worker: WorkerConfig,
    task: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Replace a draft description from the stored German sentence. The script calls GitHub.

    An order uses the full stored German sentence, not the English title.
    A sentence that is not stored yet, and a stored sentence that only
    repeats the English title, is taken from the implementer checkpoint
    and stored on the task as ``change_summary_de``. That stored value is
    the full sentence. It replaces an already published description even
    when the reused and added list is absent, including a description
    whose DE line is the English title or cannot be read. Equality with
    the line just generated does not accept that title. Without the
    reused and added list, a missing sentence does not replace the
    published description and the check does not succeed. With that
    list, a missing sentence may replace a published English title with
    the order-only line, and the check returns for that line. It does
    not replace a published German sentence. The published description
    is read back with ``gh pr view``; its DE line has to be the stored
    German sentence, or that order-only line when the sentence is missing.
    """
    c = coord(task)
    reused = task.get("reused_lines")
    added = task.get("added_lines")
    has_lists = (
        isinstance(reused, list)
        and isinstance(added, list)
        and bool(reused)
        and bool(added)
    )
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    order = source.get("kind") == ORDER_KIND
    sentence = _order_de_sentence(task) if order else None
    if order and sentence is not None:
        if task.get("change_summary_de") != sentence:
            task["change_summary_de"] = sentence
            save_task(store, task)
        from .coordinator_a38 import _summary

        sentence = _summary(task, "de")
    summary_de = sentence if order else None
    if not has_lists and not order:
        return []
    if not isinstance(source.get("repo"), str) or (
        source.get("number") is None and not order
    ):
        return []
    number = as_int(c.get("pr_number") or task.get("ref"))
    target = target_repo(task)
    if number is None or target == "":
        if has_lists:
            raise CoordinatorError("reused and added list has no draft to update")
        return []
    # Without the reused and added list, a missing sentence must not
    # overwrite a published description. With that list, the order-only
    # line may replace a published English title below.
    if order and sentence is None and not has_lists:
        raise _published_de_error()
    body = draft_pr_body(
        source,
        [str(line) for line in reused] if has_lists else None,
        [str(line) for line in added] if has_lists else None,
        summary_de,
    )
    if order and _published_de_is_english_title(task, source, body):
        raise _published_de_error()
    posted = f"posted reused and added list on {target}#{number}"
    replaced = f"replaced draft description on {target}#{number}"
    live = _published_pr_body(store, worker, runner, target, number)
    english_live = _published_de_is_english_title(task, source, live)
    if order and sentence is None:
        # A published German sentence stays. An English title, a missing
        # line, or the order-only line itself may continue into the post.
        de_live = _description_de_line(live or "")
        short_line = (
            f"Entwurf für Auftrag {str(source.get('order_id') or '')[:8]}."
        )
        if (
            live is not None
            and de_live is not None
            and not english_live
            and de_live != short_line
        ):
            raise _published_de_error()
    if _published_description_matches(live, body) and not english_live:
        _require_stored_sentence(source, live, sentence, task)
        return [posted] if has_lists else []
    # A checked pr.body row with this body is not run again. An older edit
    # that was marked done without reading the published description does
    # not count. A live description that still differs needs a new id, and
    # two checked posts that did not stick stop further edits.
    attempts = _pr_body_attempts(store, body=body, number=number, target=target)
    if attempts >= 2:
        raise _published_de_error()
    key = f"coordinator-pr-body:{task['id']}:{body}"
    if attempts:
        key = f"{key}:{attempts}"
    activity_id = str(uuid5(NAMESPACE_URL, key))
    existing = store.row("activity", activity_id)
    if (
        isinstance(existing, dict)
        and existing.get("execution_status") == "done"
        and not _description_was_checked(existing)
    ):
        key = f"{key}:checked"
        activity_id = str(uuid5(NAMESPACE_URL, key))
    queue_activity(
        store,
        activity_id=activity_id,
        session_id=worker.session_id,
        typ="pr.body",
        payload={
            "repo": target,
            "number": number,
            "body": body,
            "description_check": True,
        },
    )
    execute_github(store, runner, activity_ids=(activity_id,))
    row = store.row("activity", activity_id)
    if row is None or row.get("execution_status") != "done":
        raise _published_de_error()
    published = _published_pr_body(store, worker, runner, target, number)
    if _published_de_is_english_title(task, source, published):
        raise _published_de_error()
    if not _published_description_matches(published, body):
        raise _published_de_error()
    _require_stored_sentence(source, published, sentence, task)
    return [posted] if has_lists else [replaced]


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
        # No German sentence yet: do not post the order-only line. The
        # implementer outcome stores the sentence and refreshes then.
        replaced = (
            refresh_draft_body(store, worker, task, runner)
            if is_order(task) and _order_de_sentence(task) is not None
            else []
        )
        return [
            f"pushed head {sha[:7]} to existing PR {target}#{existing_pr}",
            *replaced,
        ]

    activity_id = str(uuid5(NAMESPACE_URL, f"coordinator-pr-open:{task['id']}:{target}:{branch}"))
    title = str(task.get("title") or branch)
    stored_de = _stored_de_summary(task, has_lists=False)
    body = draft_pr_body(source, summary_de=stored_de)
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
        # An order with no German sentence keeps the published description.
        # Refreshing it here would fail the check before implementation.
        if not is_order(task) or _order_de_sentence(task) is not None:
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
    """Open the draft pull request on an empty start commit before the first implementation lane."""
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
    """Comment on and close the task's open pull request; return "CLOSED" or "MERGED".

    A pull request that is already merged or closed is reported as it is,
    without a comment.
    """
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
