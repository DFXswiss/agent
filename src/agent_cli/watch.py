"""Watch pr.open rows and insert pr.merged when GitHub shows a merge."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from .runtime import Completed
from .store import Store, StoreError, utcnow


def _gh(argv: list[str], runner: Callable[[list[str]], Completed]) -> dict[str, Any]:
    completed = runner(argv)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh failed").strip()
        raise StoreError(detail)
    raw = completed.stdout.strip()
    if raw == "":
        raise StoreError("gh returned empty output")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise StoreError("gh output is not an object")
    return data


def _already_merged(store: Store, session_id: str, repo: str, number: int) -> bool:
    for row in store.rows("activity"):
        if row.get("type") != "pr.merged":
            continue
        if row.get("session_id") != session_id:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("repo") == repo and int(payload.get("number") or 0) == number:
            return True
    return False


def _open_target(row: dict[str, Any]) -> tuple[str, int, str] | None:
    result = row.get("result")
    if not isinstance(result, dict):
        return None
    repo = result.get("repo")
    number = result.get("number")
    url = result.get("url") or ""
    if not isinstance(repo, str) or repo == "":
        return None
    if isinstance(number, bool) or not isinstance(number, int):
        if isinstance(number, str) and number.isdigit():
            number = int(number)
        else:
            return None
    if not isinstance(url, str):
        url = ""
    return repo, number, url


def scan_merged(
    store: Store,
    runner: Callable[[list[str]], Completed],
) -> list[str]:
    """Insert pr.merged for owned pr.open rows whose GitHub PR is merged. Returns new ids."""
    created: list[str] = []
    for row in store.rows("activity"):
        if row.get("type") != "pr.open":
            continue
        if row.get("_origin_device_id") != store.device_id():
            continue
        target = _open_target(row)
        if target is None:
            continue
        repo, number, url = target
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or session_id == "":
            continue
        if _already_merged(store, session_id, repo, number):
            continue
        info = _gh(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "state,mergedAt,mergeCommit,url,number",
            ],
            runner,
        )
        state = str(info.get("state") or "").upper()
        if state != "MERGED":
            continue
        merge_commit = info.get("mergeCommit")
        sha = ""
        if isinstance(merge_commit, dict):
            oid = merge_commit.get("oid")
            if isinstance(oid, str):
                sha = oid
        elif isinstance(merge_commit, str):
            sha = merge_commit
        merged_at = info.get("mergedAt")
        if not isinstance(merged_at, str) or merged_at == "":
            merged_at = utcnow()
        seen_url = info.get("url")
        if isinstance(seen_url, str) and seen_url:
            url = seen_url
        lock_key = f"{session_id}:{repo}:{number}"
        with store._lock, store.conn.transaction():
            store.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
            if _already_merged(store, session_id, repo, number):
                continue
            activity_id = str(uuid.uuid4())
            store.write(
                "activity",
                "insert",
                activity_id,
                {
                    "id": activity_id,
                    "session_id": session_id,
                    "type": "pr.merged",
                    "payload": {
                        "repo": repo,
                        "number": number,
                        "url": url,
                        "merge_sha": sha,
                        "merged_at": merged_at,
                        "pr_open_id": row.get("id"),
                    },
                    "execution_status": "done",
                },
            )
            created.append(activity_id)
    return created
