"""U-RUN-agent-name: the automatic name of a main agent against every case of `contracts/tasks/agent-name.json`, and
the main agent's given name in the profile store (KOR-52, ADR-095 sections 1 and 2): the newest `task.agent_name` by
scope sequence number wins, null and a body that breaks the schema mean automatic. Real store, real sealed events of
the reconcile rig.
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
from agent_runner.sessions.agent_name import automatic_name
from agent_runner.store.migrate import MIGRATIONS_DIR, migrate

from ..reconcile.rig import Rig, seal, standard_world
from .kit import ORG, TASK, WG, WG_OTHER, kit

CASES = Path(__file__).resolve().parents[4] / "contracts" / "tasks" / "agent-name.json"
EARLIER = "2026-11-21T11:00:00.000Z"


@pytest.mark.parametrize("case", json.loads(CASES.read_text(encoding="utf-8"))["cases"], ids=lambda case: case["name"])
def test_automatic_name_cases(case: dict[str, str]) -> None:
    assert automatic_name(case["title"]) == case["agent_name"]


def test_the_rule_constants_are_the_contracts() -> None:
    rule = json.loads(CASES.read_text(encoding="utf-8"))
    assert automatic_name("") == rule["empty"]
    assert automatic_name("a " * 10).count(" ") + 1 == rule["max_words"]
    assert len(automatic_name("x" * 100)) == rule["max_code_points"]


def _sig() -> str:
    return str(v.sig_pk("alice"))


def test_newest_agent_name_by_scope_sequence_wins(rig: Rig) -> None:
    async def main() -> None:
        async with rig.env(standard_world()) as env:
            await kit(env)
            org = next(iter(env.world.orgs.values()))
            key = env.sync.storage_key

            def named(label: str, body: Any, scope_id: str = WG) -> dict[str, Any]:
                return seal(org, "alice", "task.agent_name", scope_id=scope_id, label=label, task=TASK, body=body)

            async def keep(event: dict[str, Any], seq: int, sig: str | None = None) -> bool:
                return await profiles.remember(
                    conn, header=event["header"], sealed=event, author_sig=sig or _sig(), scope_seq=seq
                )

            async with env.pool.connection() as conn:
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) is None
                first = named("first", {"name": "Login doctor"})
                assert await keep(first, 5)
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) == "Login doctor"
                # a later position wins even with an earlier signed time; an earlier position never replaces it
                second = named("second", {"name": "Second"})
                header = {**second["header"], "ts": EARLIER}
                assert await profiles.remember(conn, header=header, sealed=second, author_sig=_sig(), scope_seq=7)
                stale = named("stale", {"name": "Stale"})
                assert not await keep(stale, 6)
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) == "Second"
                # another workgroup's event naming the same task ID is kept apart
                other = named("other", {"name": "Other"}, scope_id=WG_OTHER)
                assert await keep(other, 9)
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) == "Second"
                # null: back to the automatic name
                back = named("back", {"name": None})
                assert await keep(back, 8)
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) is None
                # a body that breaks the schema is never a name, and the older one does not come back
                for seq, body in enumerate(
                    ({"name": "a\nb"}, {"name": "x" * 81}, {"name": ""}, {"name": "ok", "x": 1}, ["ok"]), start=10
                ):
                    bad = named(f"bad-{seq}", body)
                    assert await profiles.remember(
                        conn, header=bad["header"], sealed=bad, author_sig=_sig(), scope_seq=seq
                    )
                    assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) is None, body
                # one that does not open (wrong signature) is no name either
                last = named("last", {"name": "Fine"})
                assert await keep(last, 20, "x")
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) is None
                fine = named("fine", {"name": "x" * 80})
                assert await keep(fine, 21)
                assert await profiles.given_name(conn, key, ORG, TASK, wg_id=WG) == "x" * 80
                # a name cannot be kept without its position (it decides which one is newest)
                with pytest.raises(ValueError, match="scope_seq"):
                    await profiles.remember(conn, header=fine["header"], sealed=fine, author_sig=_sig())

    asyncio.run(main())


def test_migration_adds_the_kind_and_the_position(runner_dsn: str, tmp_path: Path) -> None:
    before = tmp_path / "m"
    before.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) < 31:
            (before / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    assert migrate(runner_dsn, before).status == "applied"
    header: dict[str, Any] = {"scope_id": WG, "kind": "task.meta"}
    insert = (
        "INSERT INTO runner.profile_event (kind, org_id, scope_id, object_id, event_id, ts, author_sig, sealed)"
        " VALUES (%s, %s, %s, %s, %s, 't', 's', %s)"
    )
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        conn.execute(insert, ("task.meta", ORG, WG, TASK, TASK, json.dumps({"header": header})))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, ("task.agent_name", ORG, WG, TASK, TASK, json.dumps({"header": header})))
    assert migrate(runner_dsn).status == "applied"
    with psycopg.connect(runner_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT kind, scope_seq FROM runner.profile_event").fetchall() == [("task.meta", None)]
        # a name without its position is refused; with one it is kept
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(insert, ("task.agent_name", ORG, WG, TASK, TASK, json.dumps({"header": header})))
        conn.execute(
            "INSERT INTO runner.profile_event"
            " (kind, org_id, scope_id, object_id, event_id, ts, author_sig, sealed, scope_seq)"
            " VALUES ('task.agent_name', %s, %s, %s, %s, 't', 's', '{}', 3)",
            (ORG, WG, TASK, TASK),
        )
