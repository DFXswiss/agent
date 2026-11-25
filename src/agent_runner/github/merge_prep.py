"""The stored preparation of a merge commit and the workspace digests (ADR-084 section 4; runner migration
`0027_merge_prep`).

**Preparation** (`runner.merge_prep`, one row per session and repository in lower case): written by `github.merge_base`
after the import (`save`, replacing an earlier row), read by `github.commit` (`latest`), marked `committed` with the
merge commit (`committed`) and `pushed` by the push of that commit (`pushed`). A later `github.checkout` of the
repository in the session removes it (`end`): the workspace was replaced. A normal commit is refused while the
session's row of the repository is `pending` or `committed` (`github.commit`).

**Workspace digest** (`runner.workspace_state`): SHA-256 over the sorted `path\\0blob\\n` lines of the regular files
(without protected names) the runner last imported into a repository directory of the session (`github.checkout`,
`github.merge_base`) or committed from it (the snapshot of a **succeeded** `github.commit`, with its commit SHA: a
committed but unpushed state is only clean while the branch head contains that commit). The workspace protection of
`github.merge_base` compares a fresh export with it.

`prune` (daemon task `retention`): rows older than `KEEP_DAYS` days.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection

from agent_runner.github import tarsafe

KEEP_DAYS = 7
PRUNE_BATCH = 500
PENDING, COMMITTED, PUSHED = "pending", "committed", "pushed"
Entry = list[str] | None
"""A tree entry `[mode, sha]` as JSON, or None for an absent path."""


@dataclass(frozen=True)
class Prep:
    id: str
    session_id: str
    repo: str
    number: int
    branch: str
    head_sha: str
    base_sha: str
    merge_base_sha: str
    changed: dict[str, Entry]
    conflicts: dict[str, str]
    sides: dict[str, dict[str, Entry]]
    markers: dict[str, list[str]]
    state: str
    commit_sha: str | None


COLUMNS = (
    "id::text, session_id::text, repo, number, branch, head_sha, base_sha, merge_base_sha, changed, conflicts, sides,"
    " markers, state, commit_sha"
)


def digest(files: Iterable[tuple[str, str]]) -> str:
    """The workspace digest of `(path, blob)` pairs (protected names are left out)."""
    lines = sorted(f"{path}\0{blob}\n" for path, blob in files if not tarsafe.protected(path))
    return hashlib.sha256("".join(lines).encode()).hexdigest()


async def save(conn: AsyncConnection[Any], prep: Prep) -> None:
    async with conn.transaction():
        await _replace(conn, prep)


async def _replace(conn: AsyncConnection[Any], prep: Prep) -> None:
    await conn.execute(
        "DELETE FROM runner.merge_prep WHERE session_id = %s AND repo = %s", (prep.session_id, prep.repo.lower())
    )
    await conn.execute(
        "INSERT INTO runner.merge_prep (id, session_id, repo, number, branch, head_sha, base_sha, merge_base_sha,"
        " changed, conflicts, sides, markers, state) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            prep.id,
            prep.session_id,
            prep.repo.lower(),
            prep.number,
            prep.branch,
            prep.head_sha,
            prep.base_sha,
            prep.merge_base_sha,
            json.dumps(prep.changed),
            json.dumps(prep.conflicts),
            json.dumps(prep.sides),
            json.dumps(prep.markers),
            PENDING,
        ),
    )


def _row(row: Any) -> Prep:
    return Prep(*row)


async def latest(conn: AsyncConnection[Any], session_id: str, repo: str) -> Prep | None:
    cur = await conn.execute(
        f"SELECT {COLUMNS} FROM runner.merge_prep WHERE session_id = %s AND repo = %s", (session_id, repo.lower())
    )
    row = await cur.fetchone()
    return None if row is None else _row(row)


async def committed(conn: AsyncConnection[Any], prep_id: str, commit_sha: str) -> None:
    await conn.execute(
        "UPDATE runner.merge_prep SET state = %s, commit_sha = %s WHERE id = %s AND state = %s",
        (COMMITTED, commit_sha, prep_id, PENDING),
    )


async def pushed(conn: AsyncConnection[Any], session_id: str, branch: str, commit_sha: str) -> None:
    """The merge commit reached the pull request's own branch (a push to another branch changes nothing)."""
    await conn.execute(
        "UPDATE runner.merge_prep SET state = %s WHERE session_id = %s AND branch = %s AND commit_sha = %s"
        " AND state = %s",
        (PUSHED, session_id, branch, commit_sha, COMMITTED),
    )


async def end(conn: AsyncConnection[Any], session_id: str, repo: str) -> None:
    await conn.execute("DELETE FROM runner.merge_prep WHERE session_id = %s AND repo = %s", (session_id, repo.lower()))


@dataclass(frozen=True)
class Known:
    """A remembered state of a repository directory: an import (`commit` None) or a committed snapshot."""

    digest: str
    commit: str | None = None
    branch: str | None = None


async def remember(conn: AsyncConnection[Any], session_id: str, directory: str, known: Known) -> None:
    await conn.execute(
        "INSERT INTO runner.workspace_state (session_id, dir, digest, commit_sha, branch) VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (session_id, dir) DO UPDATE SET digest = EXCLUDED.digest, commit_sha = EXCLUDED.commit_sha,"
        " branch = EXCLUDED.branch, updated_at = now()",
        (session_id, directory, known.digest, known.commit, known.branch),
    )


async def remembered(conn: AsyncConnection[Any], session_id: str, directory: str) -> Known | None:
    cur = await conn.execute(
        "SELECT digest, commit_sha, branch FROM runner.workspace_state WHERE session_id = %s AND dir = %s",
        (session_id, directory),
    )
    row = await cur.fetchone()
    return None if row is None else Known(*row)


async def prune(conn: AsyncConnection[Any]) -> int:
    """One batch of rows older than `KEEP_DAYS` days of both tables (in the transaction of `retention`)."""
    total = 0
    for table, column in (("merge_prep", "created_at"), ("workspace_state", "updated_at")):
        cur = await conn.execute(
            f"DELETE FROM runner.{table} WHERE ctid IN (SELECT ctid FROM runner.{table}"
            f" WHERE {column} < now() - make_interval(days => %s) LIMIT %s)",
            (KEEP_DAYS, PRUNE_BATCH),
        )
        total += cur.rowcount
    return total
