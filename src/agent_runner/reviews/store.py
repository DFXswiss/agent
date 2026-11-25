"""What the runner knows about verdicts and commits from its own operation records (ADR-090 sections 5 and 6).

The records are the runner's `runner.operation` rows - the same operations the hub has as rows, with the parameters
and results the hub cannot read:

- `session_commits(conn, session_id)`: the commits a session made (`github.commit` results) or pushed (`github.push`);
  the head-changing types of `contracts/ops/classes.json`;
- `committers(conn, wg_id, shas)`: the sessions of the workgroup that made or pushed any of `shas`;
- `verdicts(conn, wg_id, repo, number, task_id)`: the succeeded `review.verdict` operations for a pull request
  (repository compared case-insensitively), oldest first, of the workgroup or - with `task_id` - of one task.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

_COMMITS = (
    "SELECT session_id::text, result->>'commit_sha' FROM runner.operation WHERE type = 'github.commit'"
    " AND state = 'succeeded' AND result ? 'commit_sha' AND {where}"
    " UNION SELECT session_id::text, params->>'commit_sha' FROM runner.operation WHERE type = 'github.push'"
    " AND state = 'succeeded' AND {where}"
)


@dataclass(frozen=True)
class Verdict:
    verdict_id: str
    session_id: str
    task_id: str
    head_sha: str
    outcome: str
    profile: str | None
    created_at: datetime

    def as_json(self, time_text: str) -> dict[str, Any]:
        return {
            "verdict_id": self.verdict_id,
            "session_id": self.session_id,
            "profile": self.profile,
            "outcome": self.outcome,
            "head_sha": self.head_sha,
            "created_at": time_text,
        }


async def session_commits(conn: AsyncConnection[Any], session_id: str) -> set[str]:
    cursor = await conn.execute(_COMMITS.format(where="session_id = %s"), (session_id, session_id))
    return {str(row[1]).lower() for row in await cursor.fetchall() if row[1] is not None}


async def committers(conn: AsyncConnection[Any], wg_id: str, shas: set[str]) -> set[str]:
    cursor = await conn.execute(_COMMITS.format(where="wg_id = %s"), (wg_id, wg_id))
    return {str(row[0]) for row in await cursor.fetchall() if row[1] is not None and str(row[1]).lower() in shas}


async def verdicts(
    conn: AsyncConnection[Any], wg_id: str, repo: str, number: int, task_id: str | None = None
) -> list[Verdict]:
    cursor = await conn.execute(
        "SELECT id::text, session_id::text, task_id::text, params, result, created_at FROM runner.operation"
        " WHERE type = 'review.verdict' AND state = 'succeeded' AND wg_id = %s AND lower(params->>'repo') = %s"
        " AND (params->>'number')::bigint = %s AND (%s::uuid IS NULL OR task_id = %s::uuid)"
        " ORDER BY created_at, id",
        (wg_id, repo.lower(), number, task_id, task_id),
    )
    found = []
    for verdict_id, session_id, task, params, result, created_at in await cursor.fetchall():
        profile = (result or {}).get("profile")
        found.append(
            Verdict(
                verdict_id=verdict_id,
                session_id=session_id,
                task_id=task,
                head_sha=str(params["head_sha"]).lower(),
                outcome=str(params["outcome"]),
                profile=profile if isinstance(profile, str) else None,
                created_at=created_at,
            )
        )
    return found
