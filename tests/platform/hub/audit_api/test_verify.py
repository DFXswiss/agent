"""U-HUB-audit (verification and checkpoints): `GET /v1/orgs/{org}/audit/verify` with `verify_chain` and the newest
checkpoint, tampering in the database and in a copy, and the hourly checkpoint job (03-api.md section 4.11,
02-data-model.md section 2.9; R-6.7)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg

from agent_hub.audit import jobs
from agent_hub.audit.chain import audit_hash, read_audit, verify_rows
from agent_hub.audit.checkpoint import checkpoint_signature_valid, newest_checkpoint
from agent_hub.audit.jobs import CHECKPOINT_INTERVAL, hourly_checkpoint
from agent_hub.audit.query import EMPTY_HASH, verify
from agent_hub.crypto.encoding import b64u_decode, format_time
from agent_hub.jobs import Job
from agent_hub.registry import JOBS, concat_sequences
from tests.platform.hub.conftest import HubDatabase
from tests.platform.hub.monitoring.support import Watch


def verification(watch: Watch) -> dict[str, Any]:
    response = watch.admin.browser.get(f"/v1/orgs/{watch.mine.id}/audit/verify")
    assert response.status_code == 200, response.text
    return dict(response.json())


def head(watch: Watch) -> tuple[int, str]:
    with watch.hub.pool.connection() as connection:
        row = connection.execute("SELECT seq, hash FROM hub.audit ORDER BY seq DESC LIMIT 1").fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


def checkpoint_now(watch: Watch) -> Any:
    with watch.hub.pool.connection() as connection:
        return hourly_checkpoint(connection, watch.hub.config.signer(), watch.now())


def test_an_intact_chain_verifies(watch: Watch) -> None:
    seq, digest = head(watch)
    assert verification(watch) == {"ok": True, "head": {"seq": seq, "hash": digest}, "checkpoint": None}
    written = checkpoint_now(watch)
    assert written is not None
    body = verification(watch)
    assert body["ok"] is True
    assert body["checkpoint"] == {
        "hub_key_id": watch.hub.config.signer().key_id,
        "seq": written.seq,
        "hash": written.hash,
        "published_at": format_time(watch.now()),
        "signature": body["checkpoint"]["signature"],
    }
    assert b64u_decode(body["checkpoint"]["signature"]) == written.signature
    assert body["head"]["seq"] >= written.seq  # the read log of the first verification came after the checkpoint


def test_only_admins_verify(watch: Watch) -> None:
    for actor, org in ((watch.member, watch.mine.id), (watch.other.founder, watch.mine.id)):
        response = actor.browser.get(f"/v1/orgs/{org}/audit/verify")
        assert (response.status_code, response.json()) == (404, {"error": "not_found"})


def test_tampering_in_the_database_is_detected(watch: Watch, migrated_db: HubDatabase) -> None:
    checkpoint_now(watch)
    with migrated_db.admin() as admin:
        admin.execute("ALTER TABLE hub.audit DISABLE TRIGGER audit_no_change")
        admin.execute("UPDATE hub.audit SET result = 'denied' WHERE seq = 2")
    assert verification(watch)["ok"] is False


def test_a_rehashed_chain_is_caught_by_the_checkpoint(watch: Watch, migrated_db: HubDatabase) -> None:
    written = checkpoint_now(watch)
    assert written is not None
    with migrated_db.admin() as admin:
        admin.execute("ALTER TABLE hub.audit DISABLE TRIGGER audit_no_change")
        admin.execute("UPDATE hub.audit SET result = 'denied' WHERE seq = 2")
        rows = list(read_audit(admin, 1))
        prev: str | None = rows[0].hash
        for stored in rows[1:]:
            digest = audit_hash(stored.seq, stored.row, prev)
            admin.execute("UPDATE hub.audit SET prev_hash = %s, hash = %s WHERE seq = %s", (prev, digest, stored.seq))
            prev = digest
    body = verification(watch)
    assert body["ok"] is False
    assert body["checkpoint"]["hash"] == written.hash


def test_a_forged_checkpoint_is_detected(watch: Watch, migrated_db: HubDatabase) -> None:
    seq, digest = head(watch)
    with migrated_db.admin() as admin:
        admin.execute(
            "INSERT INTO hub.audit_checkpoint (hub_key_id, seq, hash, published_at, signature)"
            " VALUES (%s, %s, %s, %s, %s)",
            (watch.hub.config.signer().key_id, seq, digest, watch.now(), bytes(64)),
        )
    assert verification(watch)["ok"] is False


def test_tampering_with_a_copy_is_detected(watch: Watch) -> None:
    with watch.hub.pool.connection() as connection:
        copy = list(read_audit(connection, 1))
    assert verify_rows(copy, 1, None).ok
    changed = [*copy[:2], replace(copy[2], row=replace(copy[2].row, result="failed")), *copy[3:]]
    assert verify_rows(changed, 1, None).broken_seq == 3


def test_an_empty_audit_has_the_empty_head(writer: psycopg.Connection[Any]) -> None:
    assert verify(writer, "hub-test", bytes(32)) == {
        "ok": True,
        "head": {"seq": 0, "hash": EMPTY_HASH},
        "checkpoint": None,
    }


def test_the_checkpoint_job_writes_one_per_hour(watch: Watch) -> None:
    signer = watch.hub.config.signer()
    first = checkpoint_now(watch)
    assert first is not None
    assert checkpoint_signature_valid(signer.public_key, first)
    watch.hub.clock.advance(CHECKPOINT_INTERVAL.total_seconds() - 1)
    verification(watch)
    watch.hub.flush_read_log()  # the head moved
    assert checkpoint_now(watch) is None  # less than an hour
    watch.hub.clock.advance(1)
    second = checkpoint_now(watch)
    assert second is not None
    assert second.seq > first.seq
    watch.hub.clock.advance(timedelta(hours=5).total_seconds())
    assert checkpoint_now(watch) is None  # the head is covered
    found = {job.name: job for job in concat_sequences(JOBS) if isinstance(job, Job)}
    assert found["audit.checkpoint"].interval_seconds == jobs.CHECK_SECONDS
    verification(watch)
    watch.hub.flush_read_log()
    third = jobs.run_checkpoint(watch.hub.app.state.hub)
    assert third is not None
    with watch.hub.pool.connection() as connection:
        assert newest_checkpoint(connection, signer.key_id) == third


def test_the_checkpoint_job_on_an_empty_audit(writer: psycopg.Connection[Any], hub_config: Any, clock: Any) -> None:
    assert hourly_checkpoint(writer, hub_config.signer(), clock()) is None
