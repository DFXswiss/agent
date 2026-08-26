"""Prepare implement tasks and isolated worktrees for pending error.fix activities."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .allow import CHECKLIST_KEYS
from .runtime import Completed
from .store import Store, StoreError, utcnow

Runner = Callable[[list[str]], Completed]


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _mark(
    store: Store,
    row: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    updated = _strip(row)
    updated["execution_status"] = status
    if error is None:
        updated.pop("execution_error", None)
    else:
        updated["execution_error"] = str(error)[:500]
    if result is not None:
        updated["result"] = result
    store.write("activity", "update", updated["id"], updated)


def _repo_ok(repo: Any) -> str | None:
    if not isinstance(repo, str) or repo.count("/") != 1:
        return None
    owner, name = repo.split("/", 1)
    if not owner or not name:
        return None
    return repo


def _nonempty_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw != "":
        return raw
    return None


def _error_seen(store: Store, session_id: str, error_id: str) -> dict[str, Any]:
    row = store.row("activity", error_id)
    if (
        row is None
        or row.get("_origin_device_id") != store.device_id()
        or row.get("session_id") != session_id
        or row.get("type") != "error.seen"
    ):
        raise StoreError("error.seen not found")
    return row


def _pr_open_merged(store: Store, pr_open_id: str) -> bool:
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.merged":
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("pr_open_id") == pr_open_id:
            return True
    return False


def _error_fix_heads(store: Store, fingerprint: str) -> set[str]:
    origin = store.device_id()
    heads: set[str] = set()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "error.seen":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint:
            continue
        seen_id = _nonempty_str(row.get("id"))
        if seen_id is not None:
            heads.add(f"error-fix-{seen_id[:8]}")
    return heads


def _draft_matches(payload: dict[str, Any], fingerprint: str, heads: set[str]) -> bool:
    if payload.get("fingerprint") == fingerprint:
        return True
    head = _nonempty_str(payload.get("head"))
    return head is not None and head in heads


def _already_open_draft(store: Store, fingerprint: str) -> bool:
    origin = store.device_id()
    heads = _error_fix_heads(store, fingerprint)
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or not _draft_matches(payload, fingerprint, heads):
            continue
        status = row.get("execution_status")
        if status == "pending":
            return True
        if status == "done" and not _pr_open_merged(store, str(row.get("id") or "")):
            return True
    return False


def validate_conclusion(
    store: Store,
    session_id: str,
    typ: str,
    payload: dict[str, Any],
) -> None:
    error_id = _nonempty_str(payload.get("error_id"))
    if error_id is None:
        raise StoreError("error_id is required")
    fingerprint = _nonempty_str(payload.get("fingerprint"))
    if fingerprint is None:
        raise StoreError("fingerprint is required")
    if typ == "error.skip" and _nonempty_str(payload.get("reason")) is None:
        raise StoreError("reason is required")
    seen = _error_seen(store, session_id, error_id)
    seen_payload = seen.get("payload")
    if not isinstance(seen_payload, dict) or seen_payload.get("fingerprint") != fingerprint:
        raise StoreError("fingerprint mismatch")
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") not in ("error.skip", "error.fix"):
            continue
        inner = row.get("payload")
        if isinstance(inner, dict) and inner.get("error_id") == error_id:
            raise StoreError("conclusion already exists")
    if typ == "error.fix" and _repo_ok(seen_payload.get("repo")) is None:
        raise StoreError("unmapped-repo")
    if typ == "error.fix" and _already_open_draft(store, fingerprint):
        raise StoreError("already-open-draft")


def find_or_create_implement_task(
    store: Store,
    session_id: str,
    error_id: str,
    title: str,
    *,
    ref: str | None = None,
) -> tuple[str, bool]:
    with store.exclusive("error-fix-act:" + store.device_id()):
        return _find_or_create_implement_task(store, session_id, error_id, title, ref=ref)


def _lookup_implement_task(store: Store, session_id: str, error_id: str) -> str | None:
    origin = store.device_id()
    for row in store.rows("task"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("session_id") != session_id:
            continue
        if row.get("workflow") != "implement":
            continue
        payload = row.get("payload")
        if isinstance(payload, dict) and payload.get("error_id") == error_id:
            return str(row["id"])
    return None


def _find_or_create_implement_task(
    store: Store,
    session_id: str,
    error_id: str,
    title: str,
    *,
    ref: str | None = None,
) -> tuple[str, bool]:
    if _nonempty_str(error_id) is None:
        raise StoreError("error_id is required")
    existing = _lookup_implement_task(store, session_id, error_id)
    if existing is not None:
        return existing, False
    seen = _error_seen(store, session_id, error_id)
    seen_payload = seen.get("payload")
    repo = _repo_ok(seen_payload.get("repo") if isinstance(seen_payload, dict) else None)
    if repo is None:
        raise StoreError("unmapped-repo")
    fingerprint = _nonempty_str(
        seen_payload.get("fingerprint") if isinstance(seen_payload, dict) else None
    )
    if fingerprint is None:
        raise StoreError("fingerprint is required")
    if _already_open_draft(store, fingerprint):
        raise StoreError("already-open-draft")
    session = store.row("session", session_id)
    if session is None:
        raise StoreError("session not found")
    if session.get("status") != "active":
        raise StoreError("session is not active")
    tid = str(uuid.uuid4())
    store.write(
        "task",
        "insert",
        tid,
        {
            "id": tid,
            "session_id": session_id,
            "workflow": "implement",
            "title": title,
            "repo": repo,
            "ref": ref,
            "payload": {"error_id": error_id, "repo": repo},
            "state": "open",
            "current_round": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "change_summary_en": None,
            "change_summary_de": None,
        },
    )
    source = "runner" if session.get("kind") == "runner" else "human"
    for key in CHECKLIST_KEYS["implement"]:
        cid = str(uuid.uuid4())
        store.write(
            "checklist_item",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "key": key,
                "status": "pending",
                "evidence": None,
                "source": source,
                "deviation_declared": False,
                "deviation_granted": False,
                "granted_by": None,
                "updated_at": utcnow(),
            },
        )
    return tid, True


def _line_fingerprint(seen_payload: dict[str, Any]) -> str | None:
    raw = _nonempty_str(seen_payload.get("line_fingerprint"))
    if raw is None or not all(c in "0123456789abcdef" for c in raw) or len(raw) != 64:
        return None
    return raw


def _pending_fix(store: Store, row: dict[str, Any]) -> tuple[str, str, str]:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise StoreError("payload must be an object")
    error_id = _nonempty_str(payload.get("error_id"))
    if error_id is None:
        raise StoreError("error_id is required")
    fingerprint = _nonempty_str(payload.get("fingerprint"))
    if fingerprint is None:
        raise StoreError("fingerprint is required")
    session_id = _nonempty_str(row.get("session_id"))
    if session_id is None:
        raise StoreError("session_id is required")
    seen = _error_seen(store, session_id, error_id)
    seen_payload = seen.get("payload")
    if not isinstance(seen_payload, dict) or seen_payload.get("fingerprint") != fingerprint:
        raise StoreError("fingerprint mismatch")
    repo = _repo_ok(seen_payload.get("repo"))
    if repo is None:
        raise StoreError("unmapped-repo")
    return error_id, fingerprint, repo


def _run_git(runner: Runner, argv: list[str], fallback: str) -> str | None:
    try:
        completed = runner(argv)
    except OSError as exc:
        return str(exc)
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or completed.stdout or fallback).strip()
    return detail or fallback


def scan_error_fix(store: Store, runner: Runner) -> list[str]:
    with store.exclusive("error-fix-act:" + store.device_id()):
        return _scan_error_fix(store, runner)


def _scan_error_fix(store: Store, runner: Runner) -> list[str]:
    rows = [
        row
        for row in store.rows("activity")
        if row.get("_origin_device_id") == store.device_id()
        and row.get("type") == "error.fix"
        and row.get("execution_status") == "pending"
    ]
    rows.sort(key=lambda row: str(row.get("id") or ""))
    lines: list[str] = []
    for row in rows:
        rid = str(row.get("id") or "?")
        try:
            error_id, _fingerprint, repo = _pending_fix(store, row)
        except StoreError as exc:
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.fix {rid} error")
            continue
        session_id = str(row["session_id"])
        parent = Path(store.home) / "error-fix-work"
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        head = f"error-fix-{error_id[:8]}"
        existing = _lookup_implement_task(store, session_id, error_id)
        if existing is not None and (parent / existing / ".git").exists():
            path = parent / existing
            result = {
                "task_id": existing,
                "worktree": str(path),
                "head": head,
                "repo": repo,
            }
            _mark(store, row, status="done", result=result)
            seen = _error_seen(store, session_id, error_id)
            seen_inner = seen.get("payload")
            extra = _line_fingerprint(seen_inner if isinstance(seen_inner, dict) else {})
            suffix = f" line_fingerprint={extra}" if extra else ""
            lines.append(f"error.fix {rid} task={existing} worktree={path}{suffix}")
            continue
        staging = parent / f"pending-{rid}"
        if not (staging / ".git").exists():
            error = _run_git(
                runner,
                ["git", "clone", "--", f"https://github.com/{repo}.git", str(staging)],
                "git clone failed",
            )
            if error is not None:
                shutil.rmtree(staging, ignore_errors=True)
                lines.append(f"error.fix {rid} error")
                continue
        checkout = ["git", "-C", str(staging), "checkout", "-B", head]
        existing_task = _lookup_implement_task(store, session_id, error_id)
        if existing_task is not None:
            existing_row = store.row("task", existing_task)
            existing_ref = None
            if existing_row is not None:
                existing_ref = _nonempty_str(existing_row.get("ref"))
            if existing_ref is not None:
                checkout.append(existing_ref)
        error = _run_git(
            runner,
            checkout,
            "git checkout failed",
        )
        if error is not None:
            shutil.rmtree(staging, ignore_errors=True)
            lines.append(f"error.fix {rid} error")
            continue
        try:
            task_id, _created = _find_or_create_implement_task(
                store,
                session_id,
                error_id,
                f"error-fix {error_id[:8]}",
            )
        except StoreError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if str(exc) in {
                "already-open-draft",
                "unmapped-repo",
                "error.seen not found",
                "fingerprint mismatch",
                "fingerprint is required",
                "error_id is required",
            }:
                _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.fix {rid} error")
            continue
        path = parent / task_id
        try:
            if path.resolve() != staging.resolve():
                if path.exists() and not (path / ".git").exists():
                    shutil.rmtree(path)
                if not path.exists():
                    staging.rename(path)
                elif (path / ".git").exists():
                    shutil.rmtree(staging, ignore_errors=True)
        except OSError:
            lines.append(f"error.fix {rid} error")
            continue
        if not (path / ".git").exists():
            shutil.rmtree(staging, ignore_errors=True)
            lines.append(f"error.fix {rid} error")
            continue
        worktree = path
        result = {
            "task_id": task_id,
            "worktree": str(worktree),
            "head": head,
            "repo": repo,
        }
        _mark(store, row, status="done", result=result)
        seen = _error_seen(store, session_id, error_id)
        seen_inner = seen.get("payload")
        extra = _line_fingerprint(seen_inner if isinstance(seen_inner, dict) else {})
        suffix = f" line_fingerprint={extra}" if extra else ""
        lines.append(f"error.fix {rid} task={task_id} worktree={worktree}{suffix}")
    return lines
