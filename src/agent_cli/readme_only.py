"""Fail-closed README-only and markdown-only detection for A38.

A change set is README-only only when every path is exactly ``README.md``
or ends with ``/README.md`` (case-sensitive). A change set is markdown-only
only when every path ends with ``.md`` (case-sensitive). Unknown git
statuses, truncated GitHub inventories, or command/API errors are neither.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

README = "README.md"
MAX_FILES = 500
_STATUS_OK = frozenset({"A", "M", "D", "T"})
_STATUS_RENAME = frozenset({"R", "C"})


def is_readme_path(path: str) -> bool:
    return path == README or path.endswith("/" + README)


def is_markdown_path(path: str) -> bool:
    return path.endswith(".md")


def paths_are_readme_only(paths: Sequence[str]) -> bool:
    # Empty inventory is not README-only (fail-closed).
    if not paths:
        return False
    return all(isinstance(p, str) and is_readme_path(p) for p in paths)


def paths_are_markdown_only(paths: Sequence[str]) -> bool:
    # Empty inventory is not markdown-only (fail-closed).
    if not paths:
        return False
    return all(isinstance(p, str) and is_markdown_path(p) for p in paths)


def parse_name_status_z(blob: bytes) -> list[str] | None:
    """Parse ``git diff --name-status -z`` output.

    Real ``-z`` records are NUL-separated with no tabs: status in its own
    field, then path(s). Rename/copy may carry an optional numeric score
    (``R100``). Incomplete or unknown records return None.
    """
    if not blob:
        return []
    parts = blob.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    paths: list[str] = []
    i = 0
    while i < len(parts):
        try:
            raw = parts[i].decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not raw:
            return None
        status = raw[0]
        score = raw[1:]
        if status in _STATUS_RENAME:
            if score and not score.isdigit():
                return None
            if i + 2 >= len(parts):
                return None
            try:
                old = parts[i + 1].decode("utf-8")
                new = parts[i + 2].decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not old or not new:
                return None
            paths.extend([old, new])
            i += 3
            continue
        if status not in _STATUS_OK or score:
            return None
        if i + 1 >= len(parts):
            return None
        try:
            path = parts[i + 1].decode("utf-8")
        except UnicodeDecodeError:
            return None
        if not path:
            return None
        paths.append(path)
        i += 2
    return paths


def git_changed_paths(repo: Path, base: str, head: str) -> list[str] | None:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--name-status",
                "-z",
                "--find-renames=100%",
                f"{base}...{head}",
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return parse_name_status_z(proc.stdout)


def git_is_readme_only(repo: Path, base: str, head: str) -> bool:
    paths = git_changed_paths(repo, base, head)
    if paths is None or len(paths) > MAX_FILES:
        return False
    return paths_are_readme_only(paths)


def git_is_markdown_only(repo: Path, base: str, head: str) -> bool:
    paths = git_changed_paths(repo, base, head)
    if paths is None or len(paths) > MAX_FILES:
        return False
    return paths_are_markdown_only(paths)


def github_file_paths(entries: Sequence[Mapping[str, Any]]) -> list[str] | None:
    paths: list[str] = []
    for item in entries:
        if not isinstance(item, Mapping):
            return None
        status = item.get("status")
        name = item.get("filename")
        if not isinstance(status, str) or not isinstance(name, str) or not name:
            return None
        if status in {"added", "removed", "modified", "changed"}:
            paths.append(name)
            continue
        if status in {"renamed", "copied"}:
            previous = item.get("previous_filename")
            if not isinstance(previous, str) or not previous:
                return None
            paths.extend([previous, name])
            continue
        return None
    return paths


def github_is_readme_only(entries: Sequence[Mapping[str, Any]], *, truncated: bool) -> bool:
    if truncated:
        return False
    if len(entries) > MAX_FILES:
        return False
    paths = github_file_paths(entries)
    if paths is None:
        return False
    return paths_are_readme_only(paths)


def github_is_markdown_only(entries: Sequence[Mapping[str, Any]], *, truncated: bool) -> bool:
    if truncated:
        return False
    if len(entries) > MAX_FILES:
        return False
    paths = github_file_paths(entries)
    if paths is None:
        return False
    return paths_are_markdown_only(paths)


def list_pull_files(api: Any, repo: str, number: int) -> list[Mapping[str, Any]] | None:
    """Return PR file entries, or None when the inventory is incomplete."""
    try:
        pages = list(api.paginate(f"/repos/{repo}/pulls/{number}/files"))
    except Exception:
        return None
    if len(pages) > MAX_FILES:
        return None
    if not all(isinstance(item, Mapping) for item in pages):
        return None
    return pages  # type: ignore[return-value]


def pull_is_readme_only(api: Any, repo: str, number: int) -> bool:
    entries = list_pull_files(api, repo, number)
    if entries is None:
        return False
    return github_is_readme_only(entries, truncated=False)


def pull_is_markdown_only(api: Any, repo: str, number: int) -> bool:
    entries = list_pull_files(api, repo, number)
    if entries is None:
        return False
    return github_is_markdown_only(entries, truncated=False)
