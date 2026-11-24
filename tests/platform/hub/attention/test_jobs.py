"""U-HUB-attention (backfill): `attention.backfill` derives the record of every task without one, in batches, without
announcing anything, and is idempotent (ADR-064 section 4; KOR-39)."""

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
