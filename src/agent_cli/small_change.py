"""Deterministic small-change verdict for one implement task and a committed head.

The verdict is fail-closed: a failed git step, an unreadable inventory, a binary
file, or any excluded path keeps it ``not-small``. Paths are measured from the
merge base with the pull request base commit. The configured extra globs are read
from that same base revision, never from the head or the working tree.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .readme_only import parse_name_status_z

MAX_CHANGED_LINES = 30  # added + deleted over all files, git numstat
MAX_FILES = 3  # paths after --no-renames (a rename is a delete plus an add)
CONFIG_PATH = ".github/small-change.json"
EVIDENCE_PREFIX = "small change at "
VERDICT_SMALL = "small"
VERDICT_NOT_SMALL = "not-small"

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_RAW_MODE = re.compile(rb"[0-7]{6}")
_RAW_SHA = re.compile(rb"[0-9a-f]+")
_RAW_STATUS = re.compile(rb"[A-Z]")
# Each alphanumeric run: uppercase word before a lowercase tail, then words and digits.
_TOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

_WORKFLOW_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    ".husky/",
    ".githooks/",
)
_WORKFLOW_EXACT = frozenset(
    {
        ".github/a38.json",
        ".github/pr-guard.json",
        ".github/small-change.json",
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        ".github/codeowners",
        "codeowners",
        "docs/codeowners",
        ".gitlab-ci.yml",
        ".pre-commit-config.yaml",
    }
)
_WORKFLOW_BASENAMES = frozenset({"contributing.md", "review.md", "agents.md"})

_DEPENDENCY_BASENAMES = frozenset(
    {
        ".gitmodules",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        ".npmrc",
        ".yarnrc",
        ".yarnrc.yml",
        "bun.lockb",
        "bun.lock",
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "go.mod",
        "go.sum",
        "cargo.toml",
        "cargo.lock",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "pubspec.yaml",
        "pubspec.lock",
        "podfile",
        "podfile.lock",
        "package.swift",
        "package.resolved",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "pom.xml",
        "packages.config",
        "directory.packages.props",
        ".nvmrc",
        ".node-version",
        ".python-version",
        ".tool-versions",
    }
)
_DEPENDENCY_GLOBS = ("requirements*.txt", "constraints*.txt", "*.csproj")

_INFRA_DIRS = frozenset(
    {
        "infra",
        "infrastructure",
        "terraform",
        "helm",
        "k8s",
        "kubernetes",
        "ansible",
        "deploy",
        "deployment",
        "deployments",
    }
)
_COMPOSE_GLOBS = (
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose.yml",
    "compose.yaml",
    "compose.*.yml",
    "compose.*.yaml",
)

_MIGRATION_DIRS = frozenset({"migration", "migrations", "migrate"})

_SECURITY_TOKENS = frozenset(
    {
        "auth",
        "authn",
        "authz",
        "oauth",
        "login",
        "logout",
        "jwt",
        "token",
        "tokens",
        "key",
        "keys",
        "secret",
        "secrets",
        "credential",
        "credentials",
        "password",
        "passwords",
        "crypto",
        "encrypt",
        "decrypt",
        "cipher",
        "signature",
        "permission",
        "permissions",
        "acl",
        "rbac",
        "guard",
        "guards",
        "security",
        "csrf",
        "cors",
        "sanitize",
        "kyc",
        "aml",
        "sanction",
        "sanctions",
        "compliance",
        "payment",
        "payments",
        "payout",
        "payouts",
        "payin",
        "payins",
        "wallet",
        "wallets",
        "fee",
        "fees",
        "price",
        "prices",
        "pricing",
        "amount",
        "amounts",
        "balance",
        "balances",
        "transaction",
        "transactions",
        "transfer",
        "transfers",
        "invoice",
        "invoices",
        "billing",
        "bank",
        "banking",
        "iban",
        "swap",
        "liquidity",
        "vault",
    }
)


@dataclass(frozen=True)
class FileStat:
    path: str
    added: int | None  # None = binary
    deleted: int | None


@dataclass(frozen=True)
class Verdict:
    verdict: str  # VERDICT_SMALL | VERDICT_NOT_SMALL
    head: str  # 40 lowercase hex, "" when unknown
    base: str  # the pull request base sha (GitHub base.sha)
    merge_base: str  # 40 hex, "" when unknown
    lines: int  # sum of added + deleted over text files
    files: tuple[FileStat, ...]
    reasons: tuple[str, ...]  # empty iff verdict == small

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict,
            "head": self.head,
            "base": self.base,
            "merge_base": self.merge_base,
            "lines": self.lines,
            "files": [
                {"path": f.path, "added": f.added, "deleted": f.deleted}
                for f in self.files
            ],
            "reasons": list(self.reasons),
            "limits": {"lines": MAX_CHANGED_LINES, "files": MAX_FILES},
        }


def _reason(path: str, label: str) -> str:
    return f"excluded path {path} ({label})"


def _is_infrastructure(low: str, base: str, parts: Sequence[str]) -> bool:
    if base == "dockerfile" or base.startswith("dockerfile.") or base.endswith(".dockerfile"):
        return True
    if base == ".dockerignore":
        return True
    if any(fnmatch.fnmatchcase(base, glob) for glob in _COMPOSE_GLOBS):
        return True
    if low.endswith((".tf", ".tfvars", ".hcl")):
        return True
    if base == ".env" or base == ".envrc" or base.startswith(".env."):
        return True
    return any(part in _INFRA_DIRS for part in parts)


def _is_migration(low: str, parts: Sequence[str]) -> bool:
    if low.endswith(".sql"):
        return True
    return any(part in _MIGRATION_DIRS for part in parts)


def _is_security(path: str) -> bool:
    # The full basename: the dot already separates tokens, so ``server.key``
    # and ``session.token`` are caught just like ``server_key``.
    for component in path.split("/"):
        for token in _TOKEN.findall(component):
            if token.lower() in _SECURITY_TOKENS:
                return True
    return False


def excluded_reason(path: str, extra_globs: Sequence[str]) -> str | None:
    """Built-in and per-repository exclusion for one repo-relative path."""
    low = path.lower()
    parts = low.split("/")
    base = parts[-1]
    if (
        low.startswith(_WORKFLOW_PREFIXES)
        or low in _WORKFLOW_EXACT
        or base in _WORKFLOW_BASENAMES
    ):
        return _reason(path, "workflow")
    if base in _DEPENDENCY_BASENAMES or any(
        fnmatch.fnmatchcase(base, glob) for glob in _DEPENDENCY_GLOBS
    ):
        return _reason(path, "dependencies")
    if _is_infrastructure(low, base, parts):
        return _reason(path, "infrastructure")
    if _is_migration(low, parts):
        return _reason(path, "migrations")
    if _is_security(path):
        return _reason(path, "security or money")
    for glob in extra_globs:
        if glob.endswith("/"):
            if path.startswith(glob):
                return _reason(path, "excluded by .github/small-change.json")
        elif fnmatch.fnmatchcase(path, glob):
            return _reason(path, "excluded by .github/small-change.json")
    return None


def evaluate(
    stats: Sequence[FileStat] | None,
    paths: Sequence[str] | None,
    extra_globs: Sequence[str],
) -> list[str]:
    """All verdict reasons in the documented order; empty means small.

    Either inventory may be ``None`` when it is unreadable; the other one then
    supplies the path count, the exclusions, and the empty-diff check. The
    unreadable-inventory reasons belong to the caller, which reports each
    inventory's state exactly once.
    """
    reasons: list[str] = []
    if paths is not None:
        available_paths: Sequence[str] | None = paths
    elif stats is not None:
        available_paths = [item.path for item in stats]
    else:
        available_paths = None
    if available_paths is not None:
        if not available_paths:
            reasons.append("empty diff")
        elif len(available_paths) > MAX_FILES:
            reasons.append(f"{len(available_paths)} files > {MAX_FILES}")
    if stats is not None:
        for item in stats:
            if item.added is None or item.deleted is None:
                reasons.append(f"binary file {item.path}")
        lines = sum(
            (item.added or 0) + (item.deleted or 0)
            for item in stats
            if item.added is not None and item.deleted is not None
        )
        if lines > MAX_CHANGED_LINES:
            reasons.append(f"{lines} changed lines > {MAX_CHANGED_LINES}")
    for path in available_paths or ():
        reason = excluded_reason(path, extra_globs)
        if reason is not None:
            reasons.append(reason)
    return reasons


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None


def _invalid_config(base: str) -> tuple[None, str]:
    return None, f"invalid {CONFIG_PATH} at {base}"


def load_extra_globs(repo: Path, base: str) -> tuple[list[str] | None, str | None]:
    """Extra exclusion globs from the base revision, or (globs, error)."""
    proc = _run_git(repo, "show", f"{base}:{CONFIG_PATH}")
    if proc is None:
        return _invalid_config(base)
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", "replace")
        if "does not exist" in error or "exists on disk, but not" in error:
            return [], None
        return _invalid_config(base)
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _invalid_config(base)
    if not isinstance(data, dict) or set(data) - {"excluded_paths"}:
        return _invalid_config(base)
    raw = data.get("excluded_paths", [])
    if not isinstance(raw, list):
        return _invalid_config(base)
    globs: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            return _invalid_config(base)
        globs.append(item)
    return globs, None


def parse_numstat_z(blob: bytes) -> list[FileStat] | None:
    """Parse ``git diff --numstat -z --no-renames`` output.

    Each record is ``added\tdeleted\tpath\\0``; ``-\\t-`` marks a binary file.
    Incomplete or unreadable records return None.
    """
    if not blob:
        return []
    records = blob.split(b"\0")
    if records and records[-1] == b"":
        records = records[:-1]
    stats: list[FileStat] = []
    for record in records:
        if not record:
            return None
        chunks = record.split(b"\t", 2)
        if len(chunks) != 3:
            return None
        added_raw, deleted_raw, path_raw = chunks
        if not path_raw:
            return None
        try:
            path = path_raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if added_raw == b"-" and deleted_raw == b"-":
            stats.append(FileStat(path, None, None))
            continue
        if not added_raw.isdigit() or not deleted_raw.isdigit():
            return None
        stats.append(FileStat(path, int(added_raw), int(deleted_raw)))
    return stats


def parse_raw_z(blob: bytes) -> list[str] | None:
    """Gitlink paths from ``git diff --raw -z --no-renames`` output.

    Each record is ``:<old mode> <new mode> <old sha> <new sha> <status>\\0
    <path>\\0``. Incomplete or unreadable records return None.
    """
    if not blob:
        return []
    if not blob.endswith(b"\0"):
        return None
    fields = blob.split(b"\0")[:-1]
    if len(fields) % 2 != 0:
        return None
    gitlinks: list[str] = []
    for index in range(0, len(fields), 2):
        meta, path_raw = fields[index], fields[index + 1]
        if not meta.startswith(b":") or not path_raw:
            return None
        pieces = meta[1:].split(b" ")
        if len(pieces) != 5:
            return None
        old_mode, new_mode, old_sha, new_sha, status = pieces
        if (
            _RAW_MODE.fullmatch(old_mode) is None
            or _RAW_MODE.fullmatch(new_mode) is None
            or _RAW_SHA.fullmatch(old_sha) is None
            or _RAW_SHA.fullmatch(new_sha) is None
            or _RAW_STATUS.fullmatch(status) is None
        ):
            return None
        try:
            path = path_raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if old_mode == b"160000" or new_mode == b"160000":
            gitlinks.append(path)
    return gitlinks


def _numstat_from(repo: Path, merge_base: str, head: str) -> list[FileStat] | None:
    proc = _run_git(
        repo,
        "diff",
        "--numstat",
        "-z",
        "--no-renames",
        "--ignore-submodules=none",
        merge_base,
        head,
    )
    if proc is None or proc.returncode != 0:
        return None
    return parse_numstat_z(proc.stdout)


def _paths_from(repo: Path, merge_base: str, head: str) -> list[str] | None:
    proc = _run_git(
        repo,
        "diff",
        "--name-status",
        "-z",
        "--no-renames",
        "--ignore-submodules=none",
        merge_base,
        head,
    )
    if proc is None or proc.returncode != 0:
        return None
    return parse_name_status_z(proc.stdout)


def _gitlinks_from(repo: Path, merge_base: str, head: str) -> list[str] | None:
    proc = _run_git(
        repo,
        "diff",
        "--raw",
        "-z",
        "--no-renames",
        "--ignore-submodules=none",
        merge_base,
        head,
    )
    if proc is None or proc.returncode != 0:
        return None
    return parse_raw_z(proc.stdout)


def check_repo(repo: Path, base: str, expected_head: str) -> Verdict:
    """Measure the committed HEAD of ``repo`` against the pull request base."""
    reasons: list[str] = []
    head = ""
    merge_base = ""
    files: tuple[FileStat, ...] = ()

    status = _run_git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
    if status is None or status.returncode != 0:
        reasons.append("git status failed")
    elif status.stdout.strip():
        reasons.append("working tree is not clean; commit first")

    rev = _run_git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if rev is None or rev.returncode != 0:
        reasons.append("git rev-parse HEAD failed")
    else:
        head = rev.stdout.decode("utf-8", "replace").strip().lower()
        if head != expected_head.lower():
            reasons.append(
                f"local HEAD {head} is not the pull request head "
                f"{expected_head.lower()}; push first"
            )

    base_ok = False
    base_proc = _run_git(repo, "cat-file", "-e", f"{base}^{{commit}}")
    if base_proc is None or base_proc.returncode != 0:
        reasons.append(
            f"base commit {base} is not available locally; fetch the base branch"
        )
    else:
        base_ok = True

    if head and base_ok:
        merged = _run_git(repo, "merge-base", base, head)
        if merged is None or merged.returncode != 0 or not merged.stdout.strip():
            reasons.append("merge base not found")
        else:
            merge_base = merged.stdout.decode("utf-8", "replace").strip().lower()

    paths: list[str] | None = None
    stats: list[FileStat] | None = None
    gitlinks: list[str] | None = None
    if head and merge_base:
        paths = _paths_from(repo, merge_base, head)
        if paths is None:
            reasons.append("path inventory unreadable")
        stats = _numstat_from(repo, merge_base, head)
        if stats is None:
            reasons.append("line inventory unreadable")
        gitlinks = _gitlinks_from(repo, merge_base, head)
        if gitlinks is None:
            reasons.append("submodule inventory unreadable")
        if (
            paths is not None
            and stats is not None
            and sorted(paths) != sorted(item.path for item in stats)
        ):
            reasons.append("inventories disagree")

    extra_globs: list[str] = []
    if base_ok:
        globs, error = load_extra_globs(repo, base)
        if error is not None:
            reasons.append(error)
        elif globs is not None:
            extra_globs = globs

    if paths is not None or stats is not None:
        reasons.extend(evaluate(stats, paths, extra_globs))
    if gitlinks is not None:
        reasons.extend(f"submodule pointer {path}" for path in gitlinks)
    if stats is not None:
        files = tuple(stats)

    lines = sum(
        (item.added or 0) + (item.deleted or 0)
        for item in files
        if item.added is not None and item.deleted is not None
    )
    return Verdict(
        verdict=VERDICT_SMALL if not reasons else VERDICT_NOT_SMALL,
        head=head,
        base=base,
        merge_base=merge_base,
        lines=lines,
        files=files,
        reasons=tuple(reasons),
    )


def evidence_head(evidence: str | None) -> str | None:
    """The sha after ``EVIDENCE_PREFIX`` when the evidence is exactly prefixed."""
    if not isinstance(evidence, str) or not evidence.startswith(EVIDENCE_PREFIX):
        return None
    rest = evidence[len(EVIDENCE_PREFIX):]
    sha = rest[:40]
    if not _FULL_SHA.fullmatch(sha):
        return None
    tail = rest[40:]
    if tail and tail[0] not in (" ", ";"):
        return None
    return sha


def is_small_change_evidence(evidence: str | None) -> bool:
    return str(evidence).startswith(EVIDENCE_PREFIX)
