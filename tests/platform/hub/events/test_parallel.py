"""U-HUB-events (parallelism and signals): gapless `scope_seq` under concurrent appends and rollbacks, no deadlock
between batches that touch the same scopes in opposite order, appends wait for a running membership append, and the
`pg_notify` signal observed by a second connection only after the commit (02-data-model.md 2.3, 03-api.md 4.4)."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from agent_hub.errors import HubError
from agent_hub.events import notify
from agent_hub.events.store import append_events
from agent_hub.orgs.append import append_batch
from tests.platform.hub.conftest import HubDatabase
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import new_id

Json = dict[str, Any]


class Rollback(Exception):
    """Raised inside a transaction to undo an append."""


def _run(threads: list[threading.Thread]) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()


def _appender(scene: Scene, url: str, barrier: threading.Barrier, errors: list[BaseException]) -> Callable[..., None]:
    hub = scene.world.hub

    def append(events: list[Json], author: str, *, rollback: bool = False) -> None:
        try:
            with psycopg.connect(url) as connection:
                barrier.wait()
                try:
                    with connection.transaction():
                        append_events(
                            connection, events, author, now=hub.clock(), root_key=hub.config.platform_root_key
                        )
                        if rollback:
                            raise Rollback
                except Rollback:
                    pass
        except BaseException as error:  # reported by the test
            errors.append(error)

    return append


def test_scope_seq_is_gapless_under_parallel_appends(scene: Scene, migrated_db: HubDatabase) -> None:
    workers, per_worker = 8, 6
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    append = _appender(scene, migrated_db.writer_url, barrier, errors)
    batches = [[scene.wg_event(scene.writer) for _ in range(per_worker)] for _ in range(workers)]
    threads = [
        threading.Thread(target=append, args=(batch, scene.writer.id), kwargs={"rollback": index % 3 == 0})
        for index, batch in enumerate(batches)
    ]
    _run(threads)
    assert errors == []
    committed = sum(1 for index in range(workers) if index % 3 != 0) * per_worker
    seqs = [row[0] for row in scene.rows("SELECT scope_seq FROM hub.event WHERE scope_id = %s ORDER BY 1", scene.wg)]
    assert seqs == list(range(1, committed + 1))
    assert scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg) == [(committed,)]
    for index, batch in enumerate(batches):  # every batch got consecutive numbers (scope head locked per call)
        ids = [event["header"]["id"] for event in batch]
        found = scene.rows("SELECT scope_seq FROM hub.event WHERE id = ANY(%s::uuid[]) ORDER BY 1", ids)
        numbers = [row[0] for row in found]
        assert numbers == ([] if index % 3 == 0 else list(range(numbers[0], numbers[0] + per_worker)))


def test_opposite_scope_order_does_not_deadlock(scene: Scene, migrated_db: HubDatabase) -> None:
    rounds = 6
    barrier = threading.Barrier(2 * rounds)
    errors: list[BaseException] = []
    append = _appender(scene, migrated_db.writer_url, barrier, errors)
    profile = "member.profile"
    threads = []
    for _ in range(rounds):
        forward = [scene.wg_event(scene.writer), scene.org_event(scene.writer, profile, ref=scene.writer.id)]
        backward = [scene.org_event(scene.writer, profile, ref=scene.writer.id), scene.wg_event(scene.writer)]
        threads += [
            threading.Thread(target=append, args=(forward, scene.writer.id)),
            threading.Thread(target=append, args=(backward, scene.writer.id)),
        ]
    _run(threads)
    assert errors == []
    assert scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg) == [(2 * rounds,)]
    assert scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.org) == [(3 + 2 * rounds,)]


def test_append_waits_for_a_running_membership_append(scene: Scene, migrated_db: HubDatabase) -> None:
    hub, draft, admin = scene.world.hub, scene.draft, scene.admin
    prompt = scene.prompt(scene.writer, new_id())
    draft.add("grant", admin, subject=scene.writer.id, wg=scene.wg, access="read")
    outcome: list[str] = []

    def append() -> None:
        with psycopg.connect(migrated_db.writer_url) as connection:
            try:
                with connection.transaction():
                    append_events(
                        connection, [prompt], scene.writer.id, now=hub.clock(), root_key=hub.config.platform_root_key
                    )
                outcome.append("stored")
            except HubError as error:
                outcome.append(error.code)

    with psycopg.connect(migrated_db.writer_url) as connection, connection.transaction():
        append_batch(
            connection,
            org_id=uuid.UUID(draft.id),
            appender=admin.appender,
            expected_seq=draft.submitted,
            entries=draft.pending(),
            wraps=[],
            events=[],
            now=hub.clock(),
            root_key=hub.config.platform_root_key,
        )
        waiting = threading.Thread(target=append)
        waiting.start()
        time.sleep(0.5)
        assert waiting.is_alive()  # blocked on the org row
        assert outcome == []
    draft.submitted = len(draft.entries)
    waiting.join(timeout=30)
    assert outcome == ["author_not_writer"]


def _rolled_back(scene: Scene, writer: psycopg.Connection[Any]) -> None:
    hub = scene.world.hub
    with writer.transaction():
        event = scene.wg_event(scene.writer)
        append_events(writer, [event], scene.writer.id, now=hub.clock(), root_key=hub.config.platform_root_key)
        raise Rollback


def test_notify_reaches_a_second_connection_after_commit(scene: Scene, migrated_db: HubDatabase) -> None:
    hub = scene.world.hub
    with psycopg.connect(migrated_db.writer_url, autocommit=True) as listener:
        listener.execute(f"LISTEN {notify.CHANNEL}")
        event = scene.wg_event(scene.writer)
        with psycopg.connect(migrated_db.writer_url) as writer:
            with pytest.raises(Rollback):
                _rolled_back(scene, writer)
            with writer.transaction():
                [stored] = append_events(
                    writer, [event], scene.writer.id, now=hub.clock(), root_key=hub.config.platform_root_key
                )
                assert list(listener.notifies(timeout=0.3, stop_after=1)) == []  # not before the commit
            replay = append_events(
                writer, [event], scene.writer.id, now=hub.clock(), root_key=hub.config.platform_root_key
            )
            writer.commit()
        assert replay[0].replayed
        received = list(listener.notifies(timeout=5, stop_after=2))
    assert [item.channel for item in received] == [notify.CHANNEL]
    assert json.loads(received[0].payload) == {
        "org_id": scene.org,
        "scope": "wg",
        "scope_id": scene.wg,
        "scope_seq": 1,
    }
    parsed = notify.parse(received[0].payload)
    assert (str(parsed.org_id), parsed.scope, str(parsed.scope_id), parsed.scope_seq) == (
        scene.org,
        "wg",
        scene.wg,
        stored.scope_seq,
    )


def test_concurrent_identical_submissions_end_in_one_store_and_one_replay(
    scene: Scene, migrated_db: HubDatabase
) -> None:
    hub = scene.world.hub
    event = scene.wg_event(scene.writer)
    barrier = threading.Barrier(2)
    answers: list[bool] = []
    errors: list[BaseException] = []

    def submit() -> None:
        try:
            with psycopg.connect(migrated_db.writer_url) as connection:
                barrier.wait()
                with connection.transaction():
                    [stored] = append_events(
                        connection, [event], scene.writer.id, now=hub.clock(), root_key=hub.config.platform_root_key
                    )
                    time.sleep(0.2)  # keep the scope head while the other call waits for it
                answers.append(stored.replayed)
        except BaseException as error:  # reported by the test
            errors.append(error)

    _run([threading.Thread(target=submit) for _ in range(2)])
    assert errors == []
    assert sorted(answers) == [False, True]
    assert scene.rows("SELECT seq FROM hub.scope_head WHERE scope_id = %s", scene.wg) == [(1,)]
