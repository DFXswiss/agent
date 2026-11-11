"""Atomic local state and fixed comment text for the conflict worker."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .conflict_mandate_config import ConflictWorker
from .coordinator_common import redact
from .store import StoreError, utcnow


CONTROL_DIR = ".conflicts-control"
CHECKPOINT_OVERLAP = timedelta(minutes=5)
INITIAL_LOOKBACK = timedelta(hours=24)
TRANSIENT_BLOCK_AFTER = timedelta(hours=24)
COMMENT_LIMIT = 8000
FAILURE_TAIL_LIMIT = 1500
NO_CONFLICTS = "no conflicts"
SESSION_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class Request:
    repo: str
    number: int
    comment_id: int
    created_at: str


@dataclass
class Context:
    worker: ConflictWorker
    request: Request
    control: Path
    worktree: Path
    state_path: Path
    state: dict[str, Any] = field(default_factory=dict)
    git_runner: Any = None


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe(worker: ConflictWorker, value: object) -> str:
    cleaned = str(value)
    replacements = (
        (str(worker.workspace_root / CONTROL_DIR), "<control>"),
        (str(worker.workspace_root), "<workspace>"),
        (str(Path.home()), "<home>"),
    )
    for path, replacement in sorted(
        replacements, key=lambda item: len(item[0]), reverse=True
    ):
        if path in ("", ".", "/"):
            continue
        cleaned = cleaned.replace(path, replacement)
    cleaned = redact(cleaned, limit=COMMENT_LIMIT)
    cleaned = re.sub(
        r'(?<![\w./:-])/(?:[^\s/"\']+/)+[^\s/"\']+',
        "[local path]",
        cleaned,
    )
    cleaned = re.sub(
        r'(?<![\w./:-])~/[^\s"\']+',
        "[local path]",
        cleaned,
    )
    return cleaned


def bounded_tail(value: str, *, limit: int = FAILURE_TAIL_LIMIT) -> str:
    raw = value or ""
    candidate = raw[-(limit * 2):] if len(raw) > limit * 2 else raw
    cleaned = redact(candidate, limit=limit * 4)
    return cleaned if len(cleaned) <= limit else "…" + cleaned[-(limit - 1):]


def _comment_text(value: str) -> str:
    characters: list[str] = []
    for character in value:
        if character == "\n":
            characters.append(character)
        elif character == "\t":
            characters.append(" ")
        elif unicodedata.category(character) in ("Cc", "Cf"):
            continue
        else:
            characters.append(character)
    return "".join(characters)


def _comment_value(worker: ConflictWorker, value: object, *, limit: int) -> str:
    """Render one dynamic value as inert Markdown code after redaction."""
    cleaned = _comment_text(redact(safe(worker, value), limit=limit))
    if "\n" not in cleaned:
        single_line = cleaned.replace("`", "'")
        return f"`{single_line}`"
    longest = max((len(run) for run in re.findall(r"`+", cleaned)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{cleaned}\n{fence}"


def _comment_line(
    worker: ConflictWorker, prefix: str, value: object, *, limit: int
) -> str:
    rendered = _comment_value(worker, value, limit=limit)
    if rendered.startswith("```"):
        return f"{prefix}\n{rendered}"
    return f"{prefix} {rendered}"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def session_component(worker: ConflictWorker) -> str:
    if SESSION_COMPONENT.fullmatch(worker.session_id) is None:
        raise StoreError("session id must be a safe single path component")
    return worker.session_id


def control_dir(worker: ConflictWorker, request: Request) -> Path:
    owner, name = request.repo.split("/", 1)
    return (
        worker.workspace_root
        / CONTROL_DIR
        / session_component(worker)
        / f"{owner}__{name}"
        / f"pr-{request.number}-c{request.comment_id}"
    )


def worktree_path(worker: ConflictWorker, request: Request) -> Path:
    owner, name = request.repo.split("/", 1)
    return (
        worker.workspace_root
        / session_component(worker)
        / f"{owner}__{name}"
        / f"pr-{request.number}-c{request.comment_id}"
    )


def _same_login(left: object, right: object) -> bool:
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and left.casefold() == right.casefold()
    )


def context(
    worker: ConflictWorker,
    request: Request,
    *,
    worker_login: str,
) -> Context:
    control = control_dir(worker, request)
    state_path = control / "state.json"
    state = read_json(state_path) or {}
    if state:
        if state.get("worker_session_id") != worker.session_id or not _same_login(
            state.get("worker_login"), worker_login
        ):
            state = {}
    state.setdefault("repo", request.repo)
    state.setdefault("number", request.number)
    state.setdefault("comment_id", request.comment_id)
    state.setdefault("created_at", request.created_at)
    state.setdefault("phase", "discovered")
    state.setdefault("first_seen", utcnow())
    state.setdefault("transient_start", None)
    state.setdefault("worker_session_id", worker.session_id)
    state["worker_login"] = worker_login
    return Context(
        worker=worker,
        request=request,
        control=control,
        worktree=worktree_path(worker, request),
        state_path=state_path,
        state=state,
    )


def save_state(ctx: Context, phase: str | None = None, **extra: Any) -> None:
    if phase is not None:
        ctx.state["phase"] = phase
    ctx.state.update(extra)
    ctx.state["updated_at"] = utcnow()
    atomic_write(ctx.state_path, json.dumps(ctx.state, sort_keys=True, indent=2) + "\n")


def checkpoint_path(worker: ConflictWorker) -> Path:
    return worker.workspace_root / CONTROL_DIR / session_component(worker) / "checkpoint.json"


def read_checkpoint(worker: ConflictWorker) -> dict[str, str]:
    data = read_json(checkpoint_path(worker)) or {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def advance_checkpoint_time(worker: ConflictWorker, repo: str, created_at: str) -> None:
    if created_at == "":
        return
    current = read_checkpoint(worker)
    previous = parse_time(current.get(repo))
    candidate = parse_time(created_at)
    if candidate is not None and (previous is None or candidate > previous):
        current[repo] = iso(candidate)
        atomic_write(
            checkpoint_path(worker),
            json.dumps(current, sort_keys=True, indent=2) + "\n",
        )


def advance_checkpoint(worker: ConflictWorker, request: Request) -> None:
    advance_checkpoint_time(worker, request.repo, request.created_at)


def request_from_state(
    worker: ConflictWorker,
    data: dict[str, Any],
    worker_login: str,
) -> Request | None:
    repo = data.get("repo")
    number = data.get("number")
    comment_id = data.get("comment_id")
    created_at = data.get("created_at")
    if (
        not isinstance(repo, str)
        or repo not in worker.repositories
        or data.get("worker_session_id") != worker.session_id
        or not _same_login(data.get("worker_login"), worker_login)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or isinstance(comment_id, bool)
        or not isinstance(comment_id, int)
        or not isinstance(created_at, str)
    ):
        return None
    return Request(repo=repo, number=number, comment_id=comment_id, created_at=created_at)


def unfinished_requests(
    worker: ConflictWorker,
    worker_login: str,
) -> list[tuple[Request, dict[str, Any]]]:
    root = worker.workspace_root / CONTROL_DIR / session_component(worker)
    if not root.is_dir():
        return []
    candidates: list[tuple[datetime, Request, dict[str, Any]]] = []
    for path in root.glob("*/*/state.json"):
        data = read_json(path)
        if data is None or data.get("phase") in ("finished", "blocked"):
            continue
        request = request_from_state(worker, data, worker_login)
        if request is None:
            continue
        first = parse_time(data.get("first_seen")) or datetime.max.replace(tzinfo=timezone.utc)
        candidates.append((first, request, data))
    candidates.sort(key=lambda item: item[0])
    return [(request, data) for _, request, data in candidates]


def comment_body(
    worker: ConflictWorker,
    kind: str,
    reason: str,
    facts: list[tuple[str, str]],
) -> str:
    if kind == "blocker":
        en = "Conflict resolution is blocked. A new comment is required to try again."
        de = "Die Konfliktlösung ist blockiert. Ein neuer Kommentar ist erforderlich."
    elif kind == "no-conflicts":
        reason = f"GitHub reports {NO_CONFLICTS} for this pull request."
        en = "No merge conflicts were found. No branch or model lane was started."
        de = (
            "Es wurden keine Merge-Konflikte gefunden. "
            "Es wurde kein Branch und keine Modell-Lane gestartet."
        )
    else:
        en = "Conflict resolution completed and the rebased branch was pushed with a lease."
        de = (
            "Die Konfliktlösung ist abgeschlossen und der rebasierte "
            "Branch wurde mit Lease gepusht."
        )
    lines = [f"EN: {en}", f"DE: {de}", "", "<details>", "<summary>Facts</summary>", ""]
    if kind == "blocker":
        lines.append(
            _comment_line(worker, "- Result: blocked. Reason:", reason, limit=1500)
        )
    else:
        lines.append(_comment_line(worker, "- Result:", reason, limit=500))
    for label, value in facts:
        lines.append(_comment_line(worker, f"- {label}:", value, limit=1000))
    lines.extend(["", "</details>"])
    return "\n".join(lines)


__all__ = [
    "CHECKPOINT_OVERLAP",
    "COMMENT_LIMIT",
    "CONTROL_DIR",
    "Context",
    "FAILURE_TAIL_LIMIT",
    "INITIAL_LOOKBACK",
    "NO_CONFLICTS",
    "Request",
    "TRANSIENT_BLOCK_AFTER",
    "advance_checkpoint",
    "advance_checkpoint_time",
    "atomic_write",
    "bounded_tail",
    "comment_body",
    "context",
    "iso",
    "parse_time",
    "read_checkpoint",
    "read_json",
    "request_from_state",
    "safe",
    "save_state",
    "session_component",
    "unfinished_requests",
]
