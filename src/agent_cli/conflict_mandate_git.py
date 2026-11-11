"""Bounded Git plumbing for the comment-mandated conflict worker."""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .coordinator_common import CoordinatorError, Runner, redact
from .runtime import Completed


_CONFLICT_MARKER = re.compile(r"^(?:<{7,}|={7,}|>{7,}|\|{7,})(?=\s|$)")
_DRIVER_PATTERN = (
    r"^(filter\..*\.(clean|smudge|process)|merge\..*\.driver|"
    r"diff\..*\.(textconv|command))$"
)
_DRIVER_LINE = re.compile(r"^(\S+)(?:\s|$)")
_UNMERGED_STATES = frozenset({"UU", "AA"})
_FORBIDDEN_REF_CHARS = frozenset(
    " ~^:?*[\\\x7f" + "".join(chr(code) for code in range(0x20))
)


@dataclass(frozen=True)
class UnmergedPath:
    path: str
    state: str
    supported: bool


def _argv(worktree: str, *parts: str) -> list[str]:
    return [
        "git",
        "-C",
        worktree,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        *parts,
    ]


def _transfer_argv(worktree: str, *parts: str) -> list[str]:
    return ["git", "-C", worktree, *parts]


def _fail(completed: Completed, label: str) -> None:
    raise CoordinatorError(redact(completed.stderr or completed.stdout or f"{label} failed"))


def run(runner: Runner, worktree: str, *parts: str) -> Completed:
    return runner(_argv(worktree, *parts))


def require_ok(
    runner: Runner,
    worktree: str,
    label: str,
    *parts: str,
) -> Completed:
    completed = run(runner, worktree, *parts)
    if completed.returncode != 0:
        _fail(completed, label)
    return completed


def _driver_names(completed: Completed, label: str) -> tuple[str, ...]:
    """Return the driver names from ``git config --get-regexp`` output."""
    if completed.returncode == 1 and not (completed.stdout or "").strip():
        return ()
    if completed.returncode != 0:
        _fail(completed, label)
    names: list[str] = []
    for line in (completed.stdout or "").splitlines():
        match = _DRIVER_LINE.match(line.strip())
        if match is None:
            raise CoordinatorError(f"{label} returned an unexpected entry")
        name = match.group(1)
        if re.fullmatch(_DRIVER_PATTERN, name) is None:
            raise CoordinatorError(f"{label} returned an unexpected entry")
        names.append(name)
    return tuple(names)


def ambient_driver_names(runner: Runner) -> tuple[str, ...]:
    """Return system and global filter, merge, and diff driver names."""
    names: list[str] = []
    for scope in ("--system", "--global"):
        completed = runner([
            "git", "config", scope, "--get-regexp", _DRIVER_PATTERN,
        ])
        names.extend(_driver_names(completed, f"read {scope.lstrip('-')} Git configuration"))
    return tuple(names)


def local_driver_names(runner: Runner, worktree: str) -> tuple[str, ...]:
    """Return the driver names defined by the cloned repository configuration."""
    completed = runner(_argv(
        worktree, "config", "--local", "--get-regexp", _DRIVER_PATTERN,
    ))
    return _driver_names(completed, "read repository Git configuration")


def clone(
    runner: Runner,
    *,
    head_repo: str,
    target_repo: str,
    dest: Path,
    git_dir: Path,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    completed = runner([
        "git",
        "clone",
        "--no-checkout",
        "--separate-git-dir",
        str(git_dir),
        f"https://github.com/{head_repo}.git",
        str(dest),
    ])
    if completed.returncode != 0:
        remove_repository(git_dir)
        remove_repository(dest)
        _fail(completed, "clone head repository")
    try:
        require_ok(
            runner,
            str(dest),
            "disable hooks",
            "config",
            "core.hooksPath",
            "/dev/null",
        )
        require_ok(runner, str(dest), "rename publication remote", "remote", "rename", "origin", "publication")
        require_ok(
            runner,
            str(dest),
            "add origin remote",
            "remote",
            "add",
            "origin",
            f"https://github.com/{target_repo}.git",
        )
        record_git_file(dest)
    except (OSError, CoordinatorError):
        remove_repository(git_dir)
        remove_repository(dest)
        raise


def record_git_file(worktree: Path) -> bytes:
    """Return the exact bytes of the worktree's regular ``.git`` file."""
    path = worktree / ".git"
    if path.is_symlink() or not path.is_file():
        raise CoordinatorError("repository metadata is not a regular file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot read repository metadata: {exc}") from exc


def git_file_snapshot(worktree: Path) -> tuple[bytes, bool]:
    """Return (bytes, regular_file) without changing the worktree."""
    path = worktree / ".git"
    regular = path.is_file() and not path.is_symlink()
    try:
        return path.read_bytes(), regular
    except OSError as exc:
        raise CoordinatorError(f"cannot read repository metadata: {exc}") from exc


def verify_git_file(worktree: Path, expected: tuple[bytes, bool]) -> None:
    """Fail before running Git when the worktree's metadata file is not intact."""
    data, regular = expected
    path = worktree / ".git"
    if not regular or path.is_symlink() or not path.is_file():
        raise CoordinatorError("lane changed the repository metadata")
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(f"cannot read repository metadata: {exc}") from exc
    if actual != data:
        raise CoordinatorError("lane changed the repository metadata")


def index_fingerprint(runner: Runner, worktree: str) -> str:
    """Hash the staged entries and the unmerged entries of the index."""
    staged = require_ok(runner, worktree, "index entries", "ls-files", "--stage", "-z")
    unmerged = require_ok(runner, worktree, "unmerged index entries", "ls-files", "--unmerged", "-z")
    digest = hashlib.sha256()
    for part in (staged.stdout or "", unmerged.stdout or ""):
        digest.update(part.encode("utf-8", "surrogateescape"))
        digest.update(b"\0\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class RepositoryState:
    config: bytes | None
    config_readable: bool
    hooks: tuple[tuple[str, int], ...] | None
    hooks_readable: bool


def _read_optional_bytes(path: Path) -> tuple[bytes | None, bool]:
    try:
        return path.read_bytes(), True
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False


def repository_state(git_dir: Path) -> RepositoryState:
    """Read the configuration bytes and the hook inventory without Git."""
    config, config_readable = _read_optional_bytes(git_dir / "config")
    hooks_path = git_dir / "hooks"
    entries: list[tuple[str, int]] | None = []
    hooks_readable = True
    try:
        children = sorted(hooks_path.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        children = []
    except OSError:
        children = []
        hooks_readable = False
    for child in children:
        try:
            size = child.lstat().st_size
        except OSError:
            hooks_readable = False
            break
        entries.append((child.name, size))
    if not hooks_readable:
        entries = None
    return RepositoryState(
        config=config,
        config_readable=config_readable,
        hooks=(tuple(entries) if entries is not None else None),
        hooks_readable=hooks_readable,
    )


def repository_state_changed(before: RepositoryState, after: RepositoryState) -> bool:
    """Return whether the config bytes or the hook inventory differ.

    A file that could not be read counts as a change: the comparison then fails
    closed instead of trusting an unobserved state.
    """
    if before.config_readable and after.config_readable:
        config_changed = before.config != after.config
    else:
        config_changed = True
    if before.hooks_readable and after.hooks_readable:
        hooks_changed = before.hooks != after.hooks
    else:
        hooks_changed = True
    return config_changed or hooks_changed


def remove_repository(path: Path) -> None:
    """Remove a worktree or Git directory without invoking Git."""
    target = Path(path)
    if target.is_symlink() or target.is_file():
        target.unlink(missing_ok=True)
        return
    shutil.rmtree(target, ignore_errors=True)


def fetch_branch(
    runner: Runner,
    worktree: str,
    *,
    remote: str,
    branch: str,
) -> None:
    completed = runner(_transfer_argv(
        worktree,
        "fetch",
        "--no-tags",
        remote,
        f"refs/heads/{branch}:refs/remotes/{remote}/{branch}",
    ))
    if completed.returncode != 0:
        _fail(completed, f"fetch {remote}/{branch}")


def checkout_head(runner: Runner, worktree: str, *, branch: str) -> None:
    require_ok(
        runner,
        worktree,
        "checkout head branch",
        "checkout",
        "--force",
        "-B",
        branch,
        f"refs/remotes/publication/{branch}",
    )


def rev_parse(runner: Runner, worktree: str, ref: str) -> str:
    completed = require_ok(runner, worktree, "rev-parse", "rev-parse", ref)
    value = (completed.stdout or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise CoordinatorError(f"cannot read {ref}")
    return value


def merge_commits(runner: Runner, worktree: str, *, base: str) -> list[str]:
    completed = require_ok(
        runner,
        worktree,
        "merge commit check",
        "rev-list",
        "--merges",
        f"origin/{base}..HEAD",
    )
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def rebase(runner: Runner, worktree: str, *, base: str) -> Completed:
    # The account Git runner is responsible for the child environment. Ambient
    # GIT_EDITOR, GIT_SEQUENCE_EDITOR, VISUAL, and EDITOR can still override
    # these configuration values; see the known limits in the operator guide.
    return run(
        runner,
        worktree,
        "-c",
        "core.editor=true",
        "-c",
        "sequence.editor=true",
        "rebase",
        "--gpg-sign",
        f"origin/{base}",
    )


def rebase_continue(runner: Runner, worktree: str) -> Completed:
    # See rebase(): the account Git runner owns child-environment filtering.
    return run(
        runner,
        worktree,
        "-c",
        "core.editor=true",
        "-c",
        "sequence.editor=true",
        "rebase",
        "--continue",
    )


def rebase_complete(runner: Runner, worktree: str) -> bool:
    # Ancestry of origin/<base> is verified separately by the final-head
    # verification. A rebase is in progress when either Git marker exists.
    for name in ("rebase-merge", "rebase-apply"):
        completed = run(runner, worktree, "rev-parse", "--git-path", name)
        if completed.returncode != 0:
            return False
        marker = Path((completed.stdout or "").strip())
        if not marker.is_absolute():
            marker = Path(worktree) / marker
        if marker.exists():
            return False
    return True


def rebase_abort(runner: Runner, worktree: str) -> None:
    if not Path(worktree).exists():
        # A blocker can finish before the clone creates a worktree. There is no
        # rebase to abort in that case.
        return
    try:
        completed = run(runner, worktree, "rebase", "--abort")
    except (OSError, CoordinatorError):
        completed = None
    if completed is not None and completed.returncode == 0:
        return
    if not rebase_complete(runner, worktree):
        raise CoordinatorError("rebase abort failed")


def _unmerged_tail(line: str) -> tuple[str, str, str]:
    # Porcelain v2 unmerged records are:
    # u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
    # Ten fields precede the path. maxsplit preserves spaces in the path.
    parts = line.split(" ", 10)
    if len(parts) == 11:
        return parts[1], parts[2], parts[10]
    raise CoordinatorError("cannot parse unmerged path")


def unmerged_paths(runner: Runner, worktree: str) -> list[UnmergedPath]:
    completed = require_ok(
        runner,
        worktree,
        "unmerged paths",
        "-c",
        "core.quotePath=false",
        "status",
        "--porcelain=v2",
    )
    result: list[UnmergedPath] = []
    for raw in (completed.stdout or "").splitlines():
        line = raw.rstrip("\r")
        if not line.startswith("u "):
            continue
        state, submodule, path = _unmerged_tail(line)
        supported = (
            state in _UNMERGED_STATES
            and submodule == "N..."
            and not path.startswith('"')
            and _regular_file(worktree, path)
            and _numstat_text(runner, worktree, path)
        )
        result.append(UnmergedPath(path=path, state=state, supported=supported))
    return result


def _regular_file(worktree: str, path: str) -> bool:
    if not path or path.startswith("/") or ".." in Path(path).parts:
        return False
    target = Path(worktree) / path
    try:
        return not target.is_symlink() and target.is_file()
    except OSError:
        return False


def _numstat_text(runner: Runner, worktree: str, path: str) -> bool:
    completed = run(
        runner,
        worktree,
        "-c",
        "core.quotePath=false",
        "diff",
        "--numstat",
        "--no-ext-diff",
        "--",
        path,
    )
    if completed.returncode != 0:
        return False
    matches: list[tuple[str, str]] = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[2] == path:
            matches.append((parts[0], parts[1]))
    return bool(matches) and all(
        added.isdigit() and removed.isdigit() for added, removed in matches
    )


def changed_paths(runner: Runner, worktree: str) -> set[str]:
    """Return worktree differences against the index plus unmerged and untracked paths.

    Files that Git itself staged cleanly while replaying a commit are equal in
    the worktree and the index, so they are intentionally not reported here.
    """
    result = _diff_paths(runner, worktree, "diff", "--name-only", "-z")
    result.update(_diff_paths(runner, worktree, "ls-files", "--others", "-z"))
    result.update(
        _diff_paths(
            runner,
            worktree,
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        )
    )
    result.update(item.path for item in unmerged_paths(runner, worktree))
    return result


def _diff_paths(runner: Runner, worktree: str, *parts: str) -> set[str]:
    completed = require_ok(
        runner,
        worktree,
        "worktree changes",
        "-c",
        "core.quotePath=false",
        *parts,
    )
    return {entry for entry in (completed.stdout or "").split("\0") if entry}


def conflict_markers(worktree: str, paths: list[str]) -> list[str]:
    failed: list[str] = []
    for path in paths:
        if not _regular_file(worktree, path):
            failed.append(f"{path}: not a regular text file")
            continue
        try:
            text = (Path(worktree) / path).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            failed.append(f"{path}: not readable UTF-8 text")
            continue
        if any(_CONFLICT_MARKER.match(line.rstrip("\r")) for line in text.splitlines()):
            failed.append(f"{path}: conflict marker remains")
    return failed


def diff_check(runner: Runner, worktree: str) -> Completed:
    return run(runner, worktree, "diff", "--check")


def stage_paths(runner: Runner, worktree: str, paths: list[str]) -> None:
    require_ok(runner, worktree, "stage conflicted paths", "add", "--", *paths)


def base_is_ancestor(runner: Runner, worktree: str, *, base: str) -> bool:
    completed = run(runner, worktree, "merge-base", "--is-ancestor", f"origin/{base}", "HEAD")
    if completed.returncode not in (0, 1):
        _fail(completed, "base ancestry")
    return completed.returncode == 0


def range_commits(runner: Runner, worktree: str, *, base: str) -> list[str]:
    completed = require_ok(
        runner,
        worktree,
        "replayed commits",
        "rev-list",
        f"origin/{base}..HEAD",
    )
    shas = [line.strip().lower() for line in (completed.stdout or "").splitlines() if line.strip()]
    if any(not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in shas):
        raise CoordinatorError("cannot read replayed commit list")
    return shas


def verify_signed_range(runner: Runner, worktree: str, shas: list[str]) -> None:
    for sha in shas:
        completed = run(runner, worktree, "verify-commit", sha)
        if completed.returncode != 0:
            raise CoordinatorError(f"commit {sha[:12]} failed cryptographic signature verification")


def branch_ref_valid(ref: object) -> bool:
    """Mirror the documented ``git check-ref-format`` branch rules in Python."""
    if not isinstance(ref, str) or not ref or ref == "@":
        return False
    if any(ch in _FORBIDDEN_REF_CHARS for ch in ref):
        return False
    if ".." in ref or "@{" in ref or "//" in ref:
        return False
    if ref.startswith(("/", "-", ".")) or ref.endswith(("/", ".")):
        return False
    for component in ref.split("/"):
        if not component or component.startswith(".") or component.endswith(".lock"):
            return False
    return True


def verify_clean_worktree(runner: Runner, worktree: str) -> None:
    changed = changed_paths(runner, worktree)
    if changed:
        raise CoordinatorError(
            "worktree is not clean: " + ", ".join(sorted(changed))
        )
    completed = require_ok(
        runner,
        worktree,
        "worktree status",
        "-c",
        "core.quotePath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if (completed.stdout or "").strip("\0\r\n"):
        raise CoordinatorError("git status reports worktree changes")


def push_with_lease(
    runner: Runner,
    worktree: str,
    *,
    ref: str,
    old_sha: str,
    verified_sha: str,
) -> Completed:
    if (
        not branch_ref_valid(ref)
        or ref in {"develop", "main", "master"}
    ):
        raise CoordinatorError(f"refusing push to protected or invalid head ref {ref}")
    if re.fullmatch(r"[0-9a-fA-F]{40}", verified_sha) is None:
        raise CoordinatorError("verified sha must be 40 hexadecimal characters")
    return runner(_transfer_argv(
        worktree,
        "push",
        f"--force-with-lease=refs/heads/{ref}:{old_sha}",
        "publication",
        f"{verified_sha}:refs/heads/{ref}",
    ))


__all__ = [
    "RepositoryState",
    "UnmergedPath",
    "ambient_driver_names",
    "base_is_ancestor",
    "branch_ref_valid",
    "changed_paths",
    "checkout_head",
    "clone",
    "conflict_markers",
    "diff_check",
    "fetch_branch",
    "git_file_snapshot",
    "index_fingerprint",
    "local_driver_names",
    "merge_commits",
    "push_with_lease",
    "range_commits",
    "rebase",
    "rebase_abort",
    "rebase_continue",
    "rebase_complete",
    "record_git_file",
    "remove_repository",
    "repository_state",
    "repository_state_changed",
    "require_ok",
    "rev_parse",
    "stage_paths",
    "unmerged_paths",
    "verify_clean_worktree",
    "verify_git_file",
    "verify_signed_range",
]
