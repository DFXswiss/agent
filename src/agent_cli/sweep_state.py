"""Fingerprint state that lets the A38 all-open sweep skip unchanged pulls.

The state file is a cache, never a verdict store. It holds one SHA-256 key per
open pull request, built from the inputs the guard reads before it would
reconcile that pull request, and the exit code of the last full evaluation.
A key is only used for 24 hours. Every read failure fails open: the sweep then
runs the full ``reconcile_pull`` as before. The file is untrusted input; it is
bounded in size and every field is validated before use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .a38_errors import is_rate_limit_error
from .a38_guard import (
    collaborator_role,
    collect_comments,
    load_ready_timeline,
    resolve_runtime_revision,
)
from .a38_review import load_thread_nodes
from .pr_lifecycle import _checks
from .workflow_approval import _list_runs

STATE_VERSION = 1
MAX_STATE_BYTES = 1024 * 1024
ENTRY_TTL_S = 86400
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_NUMBER_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class StateEntry:
    key: str
    evaluated_at: int
    exit_code: int


@dataclass
class SweepContext:
    """Per-sweep caches for values the fingerprint reads once for all pulls."""

    runtime_env: Mapping[str, str] | None = None
    _branch_tips: dict[str, str | None] = field(default_factory=dict)
    _roles: dict[tuple[str, int], str | None] = field(default_factory=dict)
    _own_user_id: int | None = None
    _own_user_loaded: bool = False
    _runtime_revision: str | None = None
    _runtime_revision_loaded: bool = False

    def branch_tip(self, api: Any, repo: str, branch: str) -> str | None:
        if branch in self._branch_tips:
            return self._branch_tips[branch]
        path = f"/repos/{repo}/branches/{urllib.parse.quote(branch, safe='')}"
        data = _get(api, path)
        tip = None
        if isinstance(data, Mapping):
            commit = data.get("commit")
            sha = commit.get("sha") if isinstance(commit, Mapping) else None
            if isinstance(sha, str) and _SHA_RE.fullmatch(sha):
                tip = sha
        self._branch_tips[branch] = tip
        return tip

    def role(
        self, api: Any, repo: str, login: str, user_id: int
    ) -> str | None:
        cache_key = (login, user_id)
        if cache_key in self._roles:
            return self._roles[cache_key]
        role = collaborator_role(api, repo, login, user_id)
        self._roles[cache_key] = role
        return role

    def own_user_id(self, api: Any) -> int:
        if not self._own_user_loaded:
            user_id, _login = api.resolve_own_user()
            self._own_user_id = user_id
            self._own_user_loaded = True
        assert self._own_user_id is not None
        return self._own_user_id

    def runtime_revision(self) -> str | None:
        if not self._runtime_revision_loaded:
            try:
                self._runtime_revision = resolve_runtime_revision(self.runtime_env)
            except Exception:  # noqa: BLE001 — a missing revision fails open
                self._runtime_revision = None
            self._runtime_revision_loaded = True
        return self._runtime_revision


_UNREADABLE = object()


def _read(call: Callable[[], Any]) -> Any:
    """Run one fingerprint read; rate limits propagate, every other error fails open."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        if is_rate_limit_error(exc):
            raise
        return _UNREADABLE


def _get(api: Any, path: str) -> Any:
    def request() -> Any:
        status, data, _headers = api.request("GET", path)
        return data if status == 200 else _UNREADABLE

    return _read(request)


def _ignore(reason: str) -> None:
    print(f"a38-guard: sweep state ignored: {reason}", file=sys.stderr, flush=True)


def _entry_number(raw: object) -> int | None:
    # A pull request number has at most 10 digits. Bound the string before the
    # conversion so an over-long key cannot raise past this validator.
    if not isinstance(raw, str) or len(raw) > 10:
        return None
    if _NUMBER_RE.fullmatch(raw) is None:
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    return number if number > 0 else None


def _entry(raw: object) -> StateEntry | None:
    if not isinstance(raw, Mapping):
        return None
    key = raw.get("key")
    evaluated_at = raw.get("evaluated_at")
    exit_code = raw.get("exit_code")
    if not isinstance(key, str) or _SHA256_RE.fullmatch(key) is None:
        return None
    if type(evaluated_at) is not int:
        return None
    if type(exit_code) is not int or exit_code not in {0, 1}:
        return None
    return StateEntry(key=key, evaluated_at=evaluated_at, exit_code=exit_code)


def load_sweep_state(
    path: str | os.PathLike[str], repo: str
) -> dict[int, StateEntry]:
    """Load the state file. Any problem yields no entries and one stderr line.

    The path is opened without following a final symlink and without blocking
    on a writerless named pipe where the platform allows it, the open
    descriptor must be a regular file, and at most ``MAX_STATE_BYTES + 1``
    bytes are read from it. A file that grows or is replaced after the check
    therefore cannot exceed the limit.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        _ignore("missing file")
        return {}
    except OSError:
        _ignore("unreadable file")
        return {}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            _ignore("not a regular file")
            return {}
        if info.st_size > MAX_STATE_BYTES:
            _ignore("file too large")
            return {}
        chunks: list[bytes] = []
        remaining = MAX_STATE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        _ignore("unreadable file")
        return {}
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if len(raw) > MAX_STATE_BYTES:
        _ignore("file too large")
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        _ignore("invalid JSON")
        return {}
    if not isinstance(data, Mapping):
        _ignore("invalid JSON")
        return {}
    version = data.get("version")
    if type(version) is not int or version != STATE_VERSION:
        _ignore("unsupported version")
        return {}
    recorded_repo = data.get("repository")
    if not isinstance(recorded_repo, str) or recorded_repo.casefold() != repo.casefold():
        _ignore("repository mismatch")
        return {}
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, Mapping):
        _ignore("invalid JSON")
        return {}
    entries: dict[int, StateEntry] = {}
    for raw_number, raw_entry in raw_entries.items():
        number = _entry_number(raw_number)
        entry = _entry(raw_entry)
        if number is None or entry is None:
            _ignore("malformed entry")
            return {}
        entries[number] = entry
    return entries


def entry_usable(entry: StateEntry, now: int) -> bool:
    """0 <= now - evaluated_at < 24 hours. A future timestamp is unusable."""
    age = now - entry.evaluated_at
    return 0 <= age < ENTRY_TTL_S


def iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def save_sweep_state(
    path: str | os.PathLike[str],
    repo: str,
    numbers: list[int],
    entries: Mapping[int, StateEntry],
    *,
    now: int | None = None,
) -> None:
    """Atomically write only entries for this sweep's still-open pull requests."""
    current = int(time.time()) if now is None else now
    selected: dict[str, dict[str, Any]] = {}
    for number in numbers:
        entry = entries.get(number)
        if entry is None or not entry_usable(entry, current):
            continue
        selected[str(number)] = {
            "key": entry.key,
            "evaluated_at": entry.evaluated_at,
            "exit_code": entry.exit_code,
        }
    payload = {
        "version": STATE_VERSION,
        "repository": repo,
        "entries": selected,
    }
    state_path = Path(path)
    temporary = state_path.with_name(f".{state_path.name}.tmp-{os.getpid()}")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, state_path)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        print(
            f"a38-guard: sweep state not saved: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _typed_field(
    obj: Mapping[str, Any],
    key: str,
    expected: type,
    *,
    nullable: bool = False,
) -> Any:
    """Value of a field that must be present, or ``_UNREADABLE`` if it is not.

    A field that is absent from its object is never acceptable, even when the
    same field may explicitly be JSON null: the nullable ones return ``None``
    only when the key is present with a null value.
    """
    if key not in obj:
        return _UNREADABLE
    value = obj[key]
    if value is None and nullable:
        return None
    if expected is int:
        return value if type(value) is int else _UNREADABLE
    if expected is bool:
        return value if type(value) is bool else _UNREADABLE
    return value if isinstance(value, expected) else _UNREADABLE


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _list_item_input(item: object) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    number = _typed_field(item, "number", int)
    head = _typed_field(item, "head", Mapping)
    base = _typed_field(item, "base", Mapping)
    user = _typed_field(item, "user", Mapping)
    if number is _UNREADABLE or number <= 0:
        return None
    if head is _UNREADABLE or base is _UNREADABLE or user is _UNREADABLE:
        return None
    head_repo = _typed_field(head, "repo", Mapping)
    base_repo = _typed_field(base, "repo", Mapping)
    if head_repo is _UNREADABLE or base_repo is _UNREADABLE:
        return None
    head_sha = _typed_field(head, "sha", str)
    head_ref = _typed_field(head, "ref", str)
    head_repo_name = _typed_field(head_repo, "full_name", str)
    base_ref = _typed_field(base, "ref", str)
    base_sha = _typed_field(base, "sha", str)
    private = _typed_field(base_repo, "private", bool)
    default_branch = _typed_field(base_repo, "default_branch", str)
    user_id = _typed_field(user, "id", int)
    user_login = _typed_field(user, "login", str)
    user_type = _typed_field(user, "type", str)
    draft = _typed_field(item, "draft", bool)
    title = _typed_field(item, "title", str)
    body = _typed_field(item, "body", str, nullable=True)
    labels_raw = _typed_field(item, "labels", list)
    merge_commit_sha = _typed_field(item, "merge_commit_sha", str, nullable=True)
    required = (
        head_sha,
        head_ref,
        head_repo_name,
        base_ref,
        base_sha,
        private,
        default_branch,
        user_id,
        user_login,
        user_type,
        draft,
        title,
        body,
        labels_raw,
        merge_commit_sha,
    )
    if any(value is _UNREADABLE for value in required):
        return None
    labels: list[str] = []
    for label in labels_raw:
        name = (
            _typed_field(label, "name", str)
            if isinstance(label, Mapping)
            else _UNREADABLE
        )
        if name is _UNREADABLE:
            return None
        labels.append(name)
    return {
        "number": number,
        "head_sha": head_sha,
        "head_ref": head_ref,
        "head_repo": head_repo_name,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "private": private,
        "default_branch": default_branch,
        "user_id": user_id,
        "user_login": user_login,
        "user_type": user_type,
        "draft": draft,
        "title_sha256": _sha256_text(title),
        "body_sha256": _sha256_text("" if body is None else body),
        "labels": sorted(labels),
        "merge_commit_sha": merge_commit_sha,
    }


def _full_pull(api: Any, repo: str, number: int) -> dict[str, Any] | None:
    data = _get(api, f"/repos/{repo}/pulls/{number}")
    if not isinstance(data, Mapping):
        return None
    head = _typed_field(data, "head", Mapping)
    base = _typed_field(data, "base", Mapping)
    if head is _UNREADABLE or base is _UNREADABLE:
        return None
    mergeable = _typed_field(data, "mergeable", bool)
    head_sha = _typed_field(head, "sha", str)
    base_sha = _typed_field(base, "sha", str)
    merge_commit_sha = _typed_field(data, "merge_commit_sha", str, nullable=True)
    draft = _typed_field(data, "draft", bool)
    required = (mergeable, head_sha, base_sha, merge_commit_sha, draft)
    if any(value is _UNREADABLE for value in required):
        return None
    return {
        "mergeable": mergeable,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "merge_commit_sha": merge_commit_sha,
        "draft": draft,
    }


def _comments_rows(comments: object) -> list[list[Any]] | None:
    if not isinstance(comments, list):
        return None
    rows: list[list[Any]] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            return None
        comment_id = comment.get("id")
        user = comment.get("user")
        updated_at = comment.get("updated_at")
        if type(comment_id) is not int or not isinstance(user, Mapping):
            return None
        user_id = user.get("id")
        if type(user_id) is not int or not isinstance(updated_at, str):
            return None
        rows.append([comment_id, user_id, updated_at])
    rows.sort(key=lambda row: (row[0], row[1], row[2]))
    return rows


def _reviews_rows(reviews: object) -> list[list[Any]] | None:
    if not isinstance(reviews, list):
        return None
    rows: list[list[Any]] = []
    for review in reviews:
        if not isinstance(review, Mapping):
            return None
        review_id = _typed_field(review, "id", int)
        user = _typed_field(review, "user", Mapping)
        state = _typed_field(review, "state", str)
        submitted_at = _typed_field(review, "submitted_at", str, nullable=True)
        commit_id = _typed_field(review, "commit_id", str, nullable=True)
        if user is _UNREADABLE:
            return None
        user_id = _typed_field(user, "id", int)
        required = (review_id, state, submitted_at, commit_id, user_id)
        if any(value is _UNREADABLE for value in required):
            return None
        rows.append([review_id, user_id, state, submitted_at, commit_id])
    rows.sort(key=lambda row: tuple(row))
    return rows


def _threads_rows(threads: object) -> list[list[Any]] | None:
    if not isinstance(threads, list):
        return None
    rows: list[list[Any]] = []
    for thread in threads:
        if not isinstance(thread, Mapping):
            return None
        thread_id = thread.get("id")
        resolved = thread.get("isResolved")
        comments = thread.get("comments")
        if (
            not isinstance(thread_id, str)
            or type(resolved) is not bool
            or not isinstance(comments, Mapping)
        ):
            return None
        nodes = comments.get("nodes")
        if not isinstance(nodes, list):
            return None
        bodies: list[str] = []
        for node in nodes:
            body = node.get("body") if isinstance(node, Mapping) else None
            if not isinstance(body, str):
                return None
            bodies.append(body)
        rows.append([
            thread_id,
            resolved,
            _sha256_text("".join(bodies)),
        ])
    rows.sort(key=lambda row: tuple(row))
    return rows


def _check_rows(checks: object) -> list[list[Any]] | None:
    if not isinstance(checks, list):
        return None
    rows: list[list[Any]] = []
    for check in checks:
        if not isinstance(check, Mapping):
            return None
        name = _typed_field(check, "name", str)
        status = _typed_field(check, "status", str)
        conclusion = _typed_field(check, "conclusion", str, nullable=True)
        app = _typed_field(check, "app", Mapping, nullable=True)
        required = (name, status, conclusion, app)
        if any(value is _UNREADABLE for value in required):
            return None
        if app is None:
            app_id = None
        else:
            app_id = _typed_field(app, "id", int)
            if app_id is _UNREADABLE:
                return None
        rows.append([name, app_id, status, conclusion])
    rows.sort(
        key=lambda row: (
            row[0],
            -1 if row[1] is None else row[1],
            row[2],
            "" if row[3] is None else row[3],
        )
    )
    return rows


def _status_rows(statuses: object, own_user_id: int) -> list[list[Any]] | None:
    if not isinstance(statuses, list):
        return None
    rows: list[list[Any]] = []
    seen: set[str] = set()
    for status in statuses:  # GitHub returns the newest status first.
        if not isinstance(status, Mapping):
            return None
        context = _typed_field(status, "context", str)
        state = _typed_field(status, "state", str)
        creator = _typed_field(status, "creator", Mapping, nullable=True)
        required = (context, state, creator)
        if any(value is _UNREADABLE for value in required):
            return None
        if creator is None:
            creator_id = None
        else:
            creator_id = _typed_field(creator, "id", int)
            if creator_id is _UNREADABLE:
                return None
        if creator_id == own_user_id:
            continue
        if context in seen:
            continue
        seen.add(context)
        rows.append([context, state, creator_id])
    rows.sort(
        key=lambda row: (row[0], row[1], -1 if row[2] is None else row[2])
    )
    return rows


def _run_rows(runs: object) -> list[list[Any]] | None:
    if not isinstance(runs, list):
        return None
    rows: list[list[Any]] = []
    for run in runs:
        if not isinstance(run, Mapping):
            return None
        run_id = _typed_field(run, "id", int)
        status = _typed_field(run, "status", str)
        conclusion = _typed_field(run, "conclusion", str, nullable=True)
        run_attempt = _typed_field(run, "run_attempt", int)
        required = (run_id, status, conclusion, run_attempt)
        if any(value is _UNREADABLE for value in required):
            return None
        rows.append([run_id, status, conclusion, run_attempt])
    rows.sort(key=lambda row: row[0])
    return rows


def _ci_input(
    api: Any, repo: str, full: Mapping[str, Any], ctx: SweepContext
) -> dict[str, Any] | None:
    own_user_id = _read(lambda: ctx.own_user_id(api))
    if own_user_id is _UNREADABLE:
        return None
    head = full["head_sha"]
    merge = full["merge_commit_sha"]
    shas = [head]
    if isinstance(merge, str) and merge != head:
        shas.append(merge)
    check_rows: list[list[list[Any]]] = []
    for sha in shas:
        checks = _read(lambda sha=sha: _checks(api, repo, sha))
        if checks is _UNREADABLE:
            return None
        rows = _check_rows(checks)
        if rows is None:
            return None
        check_rows.append(rows)
    status_rows: list[list[list[Any]]] = []
    for sha in shas:
        statuses = _read(
            lambda sha=sha: api.paginate(f"/repos/{repo}/commits/{sha}/statuses")
        )
        if statuses is _UNREADABLE:
            return None
        rows = _status_rows(statuses, own_user_id)
        if rows is None:
            return None
        status_rows.append(rows)
    runs = _read(lambda: _list_runs(api, repo, {"head_sha": head}, exact=True))
    if runs is _UNREADABLE:
        return None
    run_rows = _run_rows(runs)
    if run_rows is None:
        return None
    return {
        "checks": check_rows,
        "statuses": status_rows,
        "runs": run_rows,
    }


def _roles_input(
    api: Any,
    repo: str,
    number: int,
    item: Mapping[str, Any],
    comments: object,
    reviews: object,
    ctx: SweepContext,
) -> list[list[str]] | None:
    if not isinstance(item.get("user_type"), str):
        return None
    candidates: dict[str, int] = {
        item["user_login"]: item["user_id"],
    }
    for group in (comments, reviews):
        if not isinstance(group, list):
            return None
        for entry in group:
            if not isinstance(entry, Mapping):
                return None
            user = entry.get("user")
            if not isinstance(user, Mapping):
                return None
            user_type = user.get("type")
            if not isinstance(user_type, str):
                return None
            if user_type != "User":
                continue
            login = _typed_field(user, "login", str)
            user_id = _typed_field(user, "id", int)
            if login is _UNREADABLE or user_id is _UNREADABLE:
                return None
            candidates.setdefault(login, user_id)
    timeline = _read(lambda: load_ready_timeline(api, repo, number, strict=True))
    if timeline is _UNREADABLE:
        return None
    status, actor = timeline
    if status == "unavailable":
        return None
    if actor is not None:
        actor_id, actor_login = actor
        candidates.setdefault(actor_login, actor_id)
    rows: list[list[str]] = []
    for login, user_id in candidates.items():
        role = _read(lambda login=login, user_id=user_id: ctx.role(
            api, repo, login, user_id
        ))
        if role is _UNREADABLE or role is None:
            return None
        rows.append([login, role])
    rows.sort(key=lambda row: row[0])
    return rows


def compute_sweep_key(
    api: Any, repo: str, item: object, sweep_ctx: SweepContext
) -> str | None:
    """Canonical fingerprint of one open pull request, or None when unreadable."""
    item_input = _list_item_input(item)
    if item_input is None:
        return None
    number = item_input["number"]
    full = _full_pull(api, repo, number)
    if full is None:
        return None
    if (
        full["head_sha"] != item_input["head_sha"]
        or full["base_sha"] != item_input["base_sha"]
    ):
        return None
    base_tip = sweep_ctx.branch_tip(api, repo, item_input["base_ref"])
    default_tip = sweep_ctx.branch_tip(api, repo, item_input["default_branch"])
    if base_tip is None or default_tip is None:
        return None
    runtime_revision = sweep_ctx.runtime_revision()
    if runtime_revision is None:
        return None
    comments = _read(lambda: collect_comments(api, repo, number, strict=True))
    if comments is _UNREADABLE:
        return None
    reviews = _read(lambda: api.paginate(f"/repos/{repo}/pulls/{number}/reviews"))
    if reviews is _UNREADABLE:
        return None
    threads = _read(lambda: load_thread_nodes(api, repo, number))
    if threads is _UNREADABLE or threads is None:
        return None
    comment_rows = _comments_rows(comments)
    review_rows = _reviews_rows(reviews)
    thread_rows = _threads_rows(threads)
    if comment_rows is None or review_rows is None or thread_rows is None:
        return None
    ci_input = _ci_input(api, repo, full, sweep_ctx)
    if ci_input is None:
        return None
    role_rows = _roles_input(
        api, repo, number, item_input, comments, reviews, sweep_ctx
    )
    if role_rows is None:
        return None
    payload = {
        "item": item_input,
        "full_pull": full,
        "branch_tips": {
            "base": base_tip,
            "default": default_tip,
        },
        "runtime_revision": runtime_revision,
        "comments": comment_rows,
        "reviews": review_rows,
        "threads": thread_rows,
        "ci": ci_input,
        "roles": role_rows,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
