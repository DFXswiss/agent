"""`rebuild-projections` (02-data-model.md section 1.3): every registered projection rebuilt from its sources equals
the incremental state, and a damaged projection is detected (`--check`) and repaired (rebuild)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import psycopg
import pytest

from agent_hub.backup import rebuild
from agent_hub.config import HubConfig
from agent_hub.platform.projections import rebuild as rebuild_platform
from tests.platform.hub.backup.support import Estate
from tests.platform.hub.backup.support import table_rows as table_rows_text
from tests.platform.hub.conftest import HubDatabase

DamageFn = Callable[[Estate], str]


def run(estate: Estate, *, check: bool) -> rebuild.RebuildResult:
    with estate.hub.pool.connection() as connection:
        connection.autocommit = True
        return rebuild.rebuild(connection, estate.hub.config, check=check)


def test_every_registered_rebuilder_is_run(estate: Estate) -> None:
    names = set(rebuild.rebuilders())
    assert {"orgs", "platform", "whiteboards"} <= names
    result = run(estate, check=True)
    assert set(result.rebuilt) == names
    assert result.rebuilt["orgs"] == 2
    assert result.rebuilt["whiteboards"] == len(estate.rows("SELECT id FROM hub.whiteboard_entry"))
    assert result.rebuilt["platform"] == len(estate.rows("SELECT seq FROM hub.platform_entry"))


def test_rebuild_equals_incremental_for_every_projection(estate: Estate) -> None:
    platform = estate.platform
    platform.append("role_revoke", subject=estate.scene.writer.id, role="inviter")
    platform.append("org_platform_access", org=estate.org, granted=False)
    platform.append("device_decertify", device=estate.platform_device)
    result = run(estate, check=True)
    assert result.differences == ()
    assert result.report(check=True)["ok"] is True
    repaired = run(estate, check=False)
    assert repaired.differences == ()


def _delete(table: str, where: str = "TRUE") -> DamageFn:
    def damage(estate: Estate) -> str:
        with estate.hub.pool.connection() as connection:
            connection.execute(f"DELETE FROM hub.{table} WHERE {where}")
        return table

    return damage


def _update(table: str, assignment: str) -> DamageFn:
    def damage(estate: Estate) -> str:
        with estate.hub.pool.connection() as connection:
            connection.execute(f"UPDATE hub.{table} SET {assignment}")
        return table

    return damage


DAMAGE = [
    ("org members", _delete("org_member", "role = 'member'")),
    ("workgroups", _update("wg", "archived_seq = created_seq + 100 WHERE NOT is_default")),
    # NA-08: without the `wg` rows the rows keyed by workgroup cannot be found through them any more
    ("missing workgroups", _delete("wg")),
    ("workgroups of another org", _update("wg", "org_id = gen_random_uuid() WHERE NOT is_default")),
    ("workgroup access", _update("wg_access", "access = 'read'")),
    ("workgroup devices", _delete("wg_device")),
    ("scope epochs", _delete("scope_epoch", "scope = 'wg'")),
    ("org head", _update("org", 'rotation_pending = \'[{"scope":"org","scope_id":"x"}]\'')),
    ("platform roles", _delete("platform_role")),
    ("platform access", _update("org", "platform_access = NOT platform_access")),
    ("platform devices", _update("device", "state = 'decertified' WHERE kind = 'platform'")),
    ("whiteboards", _delete("whiteboard_entry")),
]


@pytest.mark.parametrize("damage", [item[1] for item in DAMAGE], ids=[item[0] for item in DAMAGE])
def test_a_damaged_projection_is_found_and_repaired(estate: Estate, migrated_db: HubDatabase, damage: DamageFn) -> None:
    before = {name: table_rows_text(migrated_db, name) for name in rebuild_tables(estate)}
    table = damage(estate)
    assert run(estate, check=True).differences == (table,)
    assert table_rows_text(migrated_db, table) != before[table], "check rolls back"
    assert run(estate, check=False).differences == (table,)
    assert {name: table_rows_text(migrated_db, name) for name in before} == before
    assert run(estate, check=True).differences == ()


def rebuild_tables(estate: Estate) -> list[str]:
    with estate.hub.pool.connection() as connection:
        return rebuild.mutable_tables(connection)


def test_a_rebuilder_gets_the_configuration_when_it_takes_one(hub_config: HubConfig) -> None:
    seen: list[tuple[Any, ...]] = []

    def with_config(connection: Any, config: HubConfig) -> int:
        seen.append((connection, config))
        return 1

    def without(connection: Any) -> int:
        seen.append((connection,))
        return 2

    marker = cast(psycopg.Connection[Any], object())
    assert rebuild.call(with_config, marker, hub_config) == 1
    assert rebuild.call(without, marker, hub_config) == 2
    assert seen == [(marker, hub_config), (marker,)]
    assert rebuild.rebuilders()["platform"] is rebuild_platform


def test_a_held_lock_makes_the_rebuild_give_up(
    estate: Estate, migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rebuild, "LOCK_TIMEOUT_MS", 200)
    with migrated_db.connect(migrated_db.writer_role) as holder:
        holder.execute("LOCK TABLE hub.task IN ROW EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            run(estate, check=True)
