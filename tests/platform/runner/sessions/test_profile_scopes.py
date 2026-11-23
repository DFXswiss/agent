"""U-RUN-profiles: the profile store keeps the latest event per object *and scope* (KOR-03, AUD-01 A-17).

A member of workgroup B can sign a `task.meta` in B's scope that names a task ID of workgroup A. With the old key
(kind, org_id, object_id) a newer such event replaced A's stored title, so A's sessions saw none (RUN-08 decision 21).
Real store, real sealed events of the reconcile rig.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_runner.crypto.log import vectors as v
from agent_runner.sessions import profiles
from agent_runner.store.migrate import MIGRATIONS_DIR, migrate

from ..reconcile.rig import Rig, seal, standard_world
from .kit import ORG, TASK, WG, WG_OTHER, kit

LATER = "2026-11-21T13:00:00.000Z"


def _sig() -> str:
    return str(v.sig_pk("alice"))


def test_task_meta_of_another_workgroup_never_displaces_the_title(rig: Rig) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await kit(env)
            org = next(iter(env.world.orgs.values()))
            own = seal(org, "alice", "task.meta", label="own", task=TASK, body={"title": "Own title"})
            # Same task ID, signed later, but in workgroup B's scope.
            other = seal(org, "alice", "task.meta", scope_id=WG_OTHER, label="other", task=TASK, body={"title": "B"})
            other_header = {**other["header"], "ts": LATER}
            key = env.sync.storage_key
            async with env.pool.connection() as conn:
                assert await profiles.remember(conn, header=own["header"], sealed=own, author_sig=_sig())
                assert await profiles.remember(conn, header=other_header, sealed=other, author_sig=_sig())
                assert await profiles.name(conn, key, "task.meta", ORG, TASK, scope_id=WG) == "Own title"
                cur = await conn.execute(
                    "SELECT scope_id::text FROM runner.profile_event WHERE kind = 'task.meta' ORDER BY scope_id"
                )
                assert sorted(row[0] for row in await cur.fetchall()) == sorted([WG, WG_OTHER])
                # Latest wins within one scope, as before.
                newer = seal(org, "alice", "task.meta", label="newer", task=TASK, body={"title": "Renamed"})
                newer_header = {**newer["header"], "ts": LATER}
                assert await profiles.remember(conn, header=newer_header, sealed=newer, author_sig=_sig())
                assert await profiles.name(conn, key, "task.meta", ORG, TASK, scope_id=WG) == "Renamed"
                # B's title is never read for A, and a task title always needs the workgroup.
                assert await profiles.name(conn, key, "task.meta", ORG, TASK, scope_id=WG_OTHER) is None
                with pytest.raises(ValueError, match="scope_id"):
                    await profiles.name(conn, key, "task.meta", ORG, TASK)

    asyncio.run(main())


def test_org_profiles_are_read_from_the_org_scope(rig: Rig) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await kit(env)
            org = next(iter(env.world.orgs.values()))
            named = seal(org, "alice", "org.profile", scope="org", label="o", body={"name": "Org"})
            key = env.sync.storage_key
            async with env.pool.connection() as conn:
                assert await profiles.remember(conn, header=named["header"], sealed=named, author_sig=_sig())
                assert await profiles.name(conn, key, "org.profile", ORG, ORG) == "Org"
                assert await profiles.name(conn, key, "org.profile", ORG, ORG, scope_id=ORG) == "Org"
                assert await profiles.name(conn, key, "org.profile", ORG, ORG, scope_id=WG) is None
                # A row whose key names another scope than its signed header is never read (second line of defence).
                await conn.execute("UPDATE runner.profile_event SET scope_id = %s WHERE kind = 'org.profile'", (WG,))
                assert await profiles.name(conn, key, "org.profile", ORG, ORG, scope_id=WG) is None

    asyncio.run(main())


def test_migration_keys_existing_rows_by_their_scope(runner_dsn: str, tmp_path: Path) -> None:
    before = tmp_path / "m"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) < 9:
            (before / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert migrate(runner_dsn, before).status == "applied"
    header: dict[str, Any] = {"scope_id": WG, "kind": "task.meta"}
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO runner.profile_event (kind, org_id, object_id, event_id, ts, author_sig, sealed)"
            " VALUES ('task.meta', %s, %s, %s, 't', 's', %s)",
            (ORG, TASK, TASK, json.dumps({"header": header})),
        )
    assert migrate(runner_dsn).status == "applied"
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        rows = conn.execute("SELECT scope_id::text FROM runner.profile_event").fetchall()
        key = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'profile_event_pkey'"
        ).fetchone()
    assert rows == [(WG,)]
    assert key == ("PRIMARY KEY (kind, org_id, scope_id, object_id)",)
