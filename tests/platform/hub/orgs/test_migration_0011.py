"""MIG-01 for migration 0011 (KOR-34, ADR-058): on the schema of 0010 with data it adds `wg_access.inherited` and
back-fills an active inherited `read` row for every active admin on every workgroup of its org without an active row,
with `granted_seq` as the projection would write it; explicit rows, members and removed admins stay untouched."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_hub.db import migrate
from agent_hub.db.migrate import load_migrations, run_migrate
from tests.platform.hub.conftest import HubDatabase

MIGRATIONS = Path(migrate.__file__).with_name("migrations")
AT = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
ORG, OTHER_ORG = "0190f0f0-0000-7000-8000-0000000000a0", "0190f0f0-0000-7000-8000-0000000000b0"
W0, W1, W2, W_OTHER = (f"0190f0f0-0000-7000-8000-0000000000{n}" for n in ("a1", "a2", "a3", "b1"))
ALICE, CAROL, DAVE, ERIN, FRANK = (f"0190f0f0-0000-7000-8000-00000000000{n}" for n in range(1, 6))


def _previous(tmp_path: Path) -> Path:
    previous = tmp_path / "previous"
    previous.mkdir()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if not path.name.startswith("0011_"):
            shutil.copy(path, previous / path.name)
    return previous


def _seed(writer: psycopg.Connection[Any]) -> None:
    """The projections of one org as the hub before 0011 wrote them:

    - alice: founder and admin since seq 0, creator of W1 (explicit write) and W2;
    - carol: joined as member at 2, promoted at 5 (`role`), explicit write on W1 ungranted at 8 (revoked row);
    - dave: invited as admin, joined at 6; erin: member with explicit read on W1; frank: admin, removed at 9;
    - W0 default (seq 0), W1 (seq 1), W2 (seq 7, archived at 10); another org with an admin and no workgroup access.
    """
    members = [
        (ORG, ALICE, "admin", 0, None),
        (ORG, CAROL, "admin", 2, None),
        (ORG, DAVE, "admin", 6, None),
        (ORG, ERIN, "member", 3, None),
        (ORG, FRANK, "admin", 4, 9),
        (OTHER_ORG, ERIN, "admin", 0, None),
    ]
    for org, principal, role, joined, removed in members:
        writer.execute(
            "INSERT INTO hub.org_member (org_id, principal_id, kind, role, joined_seq, removed_seq)"
            " VALUES (%s, %s, 'user', %s, %s, %s)",
            (org, principal, role, joined, removed),
        )
    for wg, org, default, created, archived in [
        (W0, ORG, True, 0, None),
        (W1, ORG, False, 1, None),
        (W2, ORG, False, 7, 10),
        (W_OTHER, OTHER_ORG, True, 0, None),
    ]:
        writer.execute(
            "INSERT INTO hub.wg (id, org_id, is_default, created_seq, archived_seq) VALUES (%s, %s, %s, %s, %s)",
            (wg, org, default, created, archived),
        )
    for seq, kind, subject in [(2, "accept", CAROL), (5, "role", CAROL), (4, "role", FRANK), (8, "ungrant", CAROL)]:
        writer.execute(
            "INSERT INTO hub.membership_entry (org_id, seq, entry_text, entry_hash, type, by_id, subject_id,"
            " received_at) VALUES (%s, %s, '{}', %s, %s, %s, %s, %s)",
            (ORG, seq, f"hash-{seq}", kind, ALICE, subject, AT),
        )
    access = [
        (W0, ALICE, "write", 0, None),
        (W0, CAROL, "write", 2, None),
        (W0, DAVE, "write", 6, None),
        (W0, ERIN, "write", 3, None),
        (W0, FRANK, "write", 4, 9),
        (W1, ALICE, "write", 1, None),
        (W1, CAROL, "write", 3, 8),
        (W1, ERIN, "read", 3, None),
        (W2, ALICE, "write", 7, None),
        (W_OTHER, ERIN, "write", 0, None),
    ]
    for wg, user, level, granted, revoked in access:
        writer.execute(
            "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq, revoked_seq) VALUES (%s, %s, %s, %s, %s)",
            (wg, user, level, granted, revoked),
        )


def test_0011_back_fills_the_inherited_read_of_admins(hub_db: HubDatabase, tmp_path: Path) -> None:
    previous = _previous(tmp_path)
    count = len(load_migrations())
    assert run_migrate(hub_db.migrate_env(), directory=previous) == (f"applied {count - 1}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        _seed(writer)
    assert run_migrate(hub_db.migrate_env()) == (f"applied {count}", 0)
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        rows = writer.execute(
            "SELECT wg_id::text, user_id::text, access, inherited, granted_seq, revoked_seq FROM hub.wg_access"
            " ORDER BY 1, 2"
        ).fetchall()
        assert rows == [
            (W0, ALICE, "write", False, 0, None),
            (W0, CAROL, "write", False, 2, None),
            (W0, DAVE, "write", False, 6, None),
            (W0, ERIN, "write", False, 3, None),
            (W0, FRANK, "write", False, 4, 9),  # a removed admin gets nothing
            (W1, ALICE, "write", False, 1, None),  # explicit access stays
            (W1, CAROL, "read", True, 8, None),  # the end of the explicit access is the start of the inherited read
            (W1, DAVE, "read", True, 6, None),  # admin since its join
            (W1, ERIN, "read", False, 3, None),  # a member inherits nothing
            (W2, ALICE, "write", False, 7, None),
            (W2, CAROL, "read", True, 7, None),  # admin since 5, the workgroup since 7 (archived: still readable)
            (W2, DAVE, "read", True, 7, None),
            (W_OTHER, ERIN, "write", False, 0, None),  # admin elsewhere: only that org's workgroups
        ]
        with pytest.raises(psycopg.errors.CheckViolation):
            writer.execute("UPDATE hub.wg_access SET access = 'write' WHERE inherited")
        writer.execute(
            "INSERT INTO hub.wg_access (wg_id, user_id, access, granted_seq) VALUES (%s, %s, 'read', 1)",
            (W1, FRANK),
        )
        found = writer.execute("SELECT inherited FROM hub.wg_access WHERE wg_id = %s AND user_id = %s", (W1, FRANK))
        assert found.fetchone() == (False,)  # an older hub's insert without the column: explicit
    assert run_migrate(hub_db.migrate_env()) == (f"unchanged {count}", 0)
