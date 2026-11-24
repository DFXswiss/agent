"""U-HUB-attention (backfill): `attention.backfill` derives the record of every task without one, in batches, without
announcing anything, and is idempotent (ADR-076 section 4; KOR-39)."""

from __future__ import annotations

import pytest

from agent_hub.attention import jobs
from agent_hub.jobs import Job
from agent_hub.registry import JOBS, concat_sequences
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.attention.support import Situation, attention_frames


def test_the_job_is_discovered() -> None:
    found = [job for job in concat_sequences(JOBS) if job.name == "attention.backfill"]
    assert len(found) == 1
    assert isinstance(found[0], Job)
    assert found[0].run is jobs.backfill


def test_backfill_derives_missing_records_in_batches(
    situation: Situation, published: MemoryPublisher, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "BATCH", 2)
    known, session = situation.working()
    situation.step(session, "idle")
    kept = situation.record(known)
    wg, owner = situation.desk.wg, situation.scene.writer.id
    older = [situation.insert_task(wg, owner, state=state) for state in ("working", "idle", "closed", "failed", "idle")]
    published.clear()
    context = situation.hub.app.state.hub
    assert jobs.backfill(context) == len(older)
    assert situation.record(known) == kept
    kinds = [situation.kind(task) for task in older]
    assert kinds == [
        ("working", "working"),
        ("done", "idle"),
        ("done", "closed"),
        ("blocked", "session_failed"),
        ("done", "idle"),
    ]
    assert attention_frames(published) == []
    assert jobs.backfill(context) == 0


def test_the_recheck_job_is_discovered() -> None:
    found = [job for job in concat_sequences(JOBS) if job.name == "attention.recheck"]
    assert len(found) == 1
    assert found[0].run is jobs.recheck


def test_recheck_follows_the_device_assignment_of_a_waiting_task(
    situation: Situation, published: MemoryPublisher
) -> None:
    wg, owner = situation.desk.wg, situation.scene.writer.id
    waiting = situation.insert_task(wg, owner, state="scheduling")
    idle = situation.insert_task(wg, owner, state="idle")
    context = situation.hub.app.state.hub
    jobs.backfill(context)
    assert situation.kind(waiting) == ("working", "queued")
    published.clear()
    assert jobs.recheck(context) == 1
    assert attention_frames(published) == []
    situation.desk.execute("UPDATE hub.wg_device SET unassigned_seq = 1 WHERE wg_id = %s", wg)
    assert jobs.recheck(context) == 1
    assert situation.kind(waiting) == ("needs_decision", "device_missing")
    assert situation.kind(idle) == ("done", "idle")
    assert [data["task_id"] for _, data in attention_frames(published)] == [waiting]


def test_a_device_of_the_whole_organization_counts_as_assigned(situation: Situation) -> None:
    """KOR-41: a device given to the organization serves every workgroup; a waiting task does not need one."""
    wg, owner = situation.desk.wg, situation.scene.writer.id
    waiting = situation.insert_task(wg, owner, state="scheduling")
    situation.desk.execute("UPDATE hub.wg_device SET unassigned_seq = 1 WHERE wg_id = %s", wg)
    context = situation.hub.app.state.hub
    jobs.backfill(context)
    assert situation.kind(waiting) == ("needs_decision", "device_missing")
    situation.desk.execute(
        "INSERT INTO hub.org_device (org_id, device_id, assigned_seq) VALUES (%s, %s, 1)",
        situation.desk.org,
        situation.scene.device.id,
    )
    jobs.recheck(context)
    assert situation.kind(waiting) == ("working", "queued")
