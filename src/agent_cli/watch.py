"""Watch pr.open rows and insert pr.merged when GitHub shows a merge."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from .runtime import Completed
from .store import Store, StoreError, utcnow


def _gh(argv: list[str], runner: Callable[[list[str]], Completed]) -> dict[str, Any]:
    try:
        completed = runner(argv)
    except OSError as exc:
        raise StoreError(f"gh is not available: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh failed").strip()
        raise StoreError(detail)
    raw = completed.stdout.strip()
    if raw == "":
        raise StoreError("gh returned empty output")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreError("gh returned invalid JSON") from exc
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
        raw_n = payload.get("number")
        if isinstance(raw_n, bool) or not isinstance(raw_n, int):
            if isinstance(raw_n, str) and raw_n.isdigit():
                raw_n = int(raw_n)
            else:
                continue
        if payload.get("repo") == repo and raw_n == number:
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
) -> tuple[list[str], int]:
    """Insert pr.merged for owned pr.open rows whose GitHub PR is merged."""
    created: list[str] = []
    skipped = 0
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
        try:
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
        except (StoreError, json.JSONDecodeError):
            skipped += 1
            continue
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
            skipped += 1
            continue
        if sha == "":
            skipped += 1
            continue
        seen_url = info.get("url")
        if isinstance(seen_url, str) and seen_url:
            url = seen_url
        lock_key = f"{session_id}:{repo}:{number}"
        activity_id = str(uuid.uuid4())
        event = store.write_with_advisory(
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
            lock_key=lock_key,
            skip=lambda: _already_merged(store, session_id, repo, number),
        )
        if event is not None:
            created.append(activity_id)
    return created, skipped
