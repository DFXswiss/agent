"""U-HUB-audit (ADR-063 section 4): the session's own events get no `event.append` row each; the audit job summarises
them per session and scope (`agent_hub.audit.eventbatch`), and migration 0015 starts the cover of existing scopes at
their head."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit import eventbatch
from agent_hub.audit.chain import verify_chain
from agent_hub.audit.eventbatch import BATCHED_KINDS, run_summary, summarise
from agent_hub.audit.jobs import JOBS
from agent_hub.db import migrate
from agent_hub.db.migrate import run_migrate
from tests.platform.hub.conftest import HubDatabase
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id

SUMMARY = (
    "SELECT actor_kind, device_id::text, session_id::text, task_id::text, target_id, detail FROM hub.audit"
    " WHERE action = 'event.append_batch' ORDER BY seq"
)


def covered(scene: Scene) -> list[tuple[Any, ...]]:
    return scene.rows("SELECT covered_seq FROM hub.event_audit_cover WHERE scope_id = %s", scene.wg)


def test_the_job_is_registered() -> None:
    [job] = [job for job in JOBS if job.name == "audit.event_batch"]
    assert (job.interval_seconds, job.run) == (eventbatch.INTERVAL_SECONDS, run_summary)
    assert {"session.output", "session.terminal", "session.tool", "session.hook", "session.file"} == BATCHED_KINDS


def test_session_events_are_summarised_per_session_once(scene: Scene) -> None:
    task_a, session_a = scene.start_session()
    task_b, session_b = scene.start_session()
    output, terminal, tool, params = scene.append(
        scene.device,
        scene.session_event(scene.device, session_a),
        scene.session_event(scene.device, session_b, "session.terminal"),
        scene.session_event(scene.device, session_a, "session.tool"),
        scene.wg_event(scene.device, "operation.params", ref=new_id()),
    )
    own = "SELECT event_id::text FROM hub.audit WHERE action = 'event.append' AND event_id = ANY(%s::uuid[])"
    assert scene.rows(own, [output.id, terminal.id, tool.id, params.id]) == [(params.id,)]
    hub = scene.world.hub
    assert run_summary(hub.app.state.hub) == 2
    rows = scene.rows(SUMMARY)
    device = scene.device.id
    assert [(kind, dev, session, task, target) for kind, dev, session, task, target, _detail in rows] == [
        ("device", device, session_a, task_a, output.id),
        ("device", device, session_b, task_b, terminal.id),
    ]
    detail_a, detail_b = rows[0][5], rows[1][5]
    assert (detail_a["scope"], detail_a["count"], detail_a["first_scope_seq"], detail_a["last_scope_seq"]) == (
        "wg",
        2,
        output.scope_seq,
        tool.scope_seq,
    )
    assert (detail_b["count"], detail_b["first_scope_seq"], detail_b["last_scope_seq"]) == (
        1,
        terminal.scope_seq,
        terminal.scope_seq,
    )
    assert detail_a["ct_bytes"] > detail_b["ct_bytes"] > 0
    assert covered(scene) == [(params.scope_seq,)]
    # nothing new: nothing written, nothing counted twice
    with hub.pool.connection() as connection:
        assert summarise(connection, hub.clock()) == 0
    [file] = scene.append(scene.device, scene.session_event(scene.device, session_a, "session.file"))
    with hub.pool.connection() as connection:
        assert summarise(connection, hub.clock()) == 1
        assert verify_chain(connection).ok
    assert scene.rows(SUMMARY)[-1][5]["first_scope_seq"] == file.scope_seq
    assert covered(scene) == [(file.scope_seq,)]


def test_a_pass_takes_at_most_max_scopes(scene: Scene) -> None:
    _task, session = scene.start_session()
    scene.append(scene.device, scene.session_event(scene.device, session))
    hub = scene.world.hub
    with hub.pool.connection() as connection:
        moved = connection.execute(
            "SELECT count(*) FROM hub.scope_head h LEFT JOIN hub.event_audit_cover c USING (scope_id)"
            " WHERE h.seq > COALESCE(c.covered_seq, 0)"
        ).fetchone()
        assert moved is not None
        assert moved[0] >= 2
        assert summarise(connection, hub.clock(), max_scopes=1) <= 1
        later = connection.execute(
            "SELECT count(*) FROM hub.scope_head h LEFT JOIN hub.event_audit_cover c USING (scope_id)"
            " WHERE h.seq > COALESCE(c.covered_seq, 0)"
        ).fetchone()
        assert later == (moved[0] - 1,)


def test_migration_0015_starts_existing_scopes_at_their_head(hub_db: HubDatabase, tmp_path: Path) -> None:
    source = Path(migrate.__file__).with_name("migrations")
    before = tmp_path / "before"
    before.mkdir()
    for path in source.glob("*.sql"):
        if int(path.name[:4]) <= 14:
            shutil.copy(path, before / path.name)
    assert run_migrate(hub_db.migrate_env(), directory=before)[1] == 0
    scope = UUID("0193000a-0000-7000-8000-0000000000bb")
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        writer.execute("INSERT INTO hub.scope_head (scope_id, scope, seq) VALUES (%s, 'wg', 7)", (scope,))
    assert run_migrate(hub_db.migrate_env())[1] == 0
    with hub_db.connect(hub_db.writer_role, autocommit=True) as writer:
        rows = writer.execute("SELECT scope_id, covered_seq FROM hub.event_audit_cover").fetchall()
        assert rows == [(scope, 7)]
        assert summarise(writer, _now(writer)) == 0


def _now(connection: psycopg.Connection[Any]) -> Any:
    row = connection.execute("SELECT now()").fetchone()
    assert row is not None
    return row[0]
