"""Helpers of the tests of the record in the hub (RECORD-PROTOCOL.md sections 7 to 9, 11.4, 12 to 14).

A hub here is the real application of `create_app`, started by `start_record` against the real
PostgreSQL of AGENT_CORE_TEST_POSTGRES, with a fixed wall clock, a fixed monotonic clock and test
points that do nothing until a test sets them. Requests are sent straight to the ASGI app with the
helpers of the signed-request tests, so every answer is checked with the hub signature.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Sequence

import yaml
from fastapi import FastAPI

from agent_core.app import create_app
from agent_core.config import Config
from agent_core.db import Store
from agent_core.github import FakeGitHub
from agent_core.record.event import Change, build_event_text
from agent_core.record.runtime import Hooks, RecordRuntime, start_record
from tests.conftest import RecordDatabase
from tests.test_signed_requests import (
    NOW,
    Answer,
    FixedClock,
    Wire,
    assert_signed,
    build_wire,
    fmt,
    private,
    public_text,
    send,
)

TEAM = "example"
OTHER_TEAM = "ops"
REPO = "example-org/example-repo"
OTHER_REPO = "example-org/other-repo"
LINK = f"{REPO}#42"
OTHER_LINK = f"{OTHER_REPO}#7"
JSON = ((b"content-type", b"application/json"),)

ALICE = "alice-laptop-1"
BOB = "bob-laptop-1"
DAVE = "dave-laptop-1"
CAROL = "carol-laptop-1"
WATCHER = "watch-one"
GUARD = "guard-one"
READER = "reader-one"
SEEDS = {ALICE: 31, BOB: 32, DAVE: 33, CAROL: 34, WATCHER: 35, GUARD: 36, READER: 37}
HANDLES = {ALICE: "alice", BOB: "bob", DAVE: "dave", CAROL: "carol"}

RECORD_UNVERIFIED = b'{"error":"record_unverified"}'
INTERNAL = b'{"error":"internal"}'


def _agent(key_id: str, team: str, scopes: Sequence[str]) -> dict[str, Any]:
    return {
        "id": key_id,
        "kind": "ed25519",
        "principal": "agent",
        "handle": HANDLES[key_id],
        "team": team,
        "device": "laptop-1",
        "public_key": public_text(SEEDS[key_id]),
        "scopes": list(scopes),
    }


def _service(key_id: str, handle: str, scopes: Sequence[str], subjects: Sequence[str]) -> dict[str, Any]:
    return {
        "id": key_id,
        "kind": "ed25519",
        "principal": "service",
        "handle": handle,
        "team": TEAM,
        "device": f"runner-{SEEDS[key_id]}",
        "public_key": public_text(SEEDS[key_id]),
        "scopes": list(scopes),
        "subjects": list(subjects),
        "expires": fmt(NOW + 365 * 86400),
    }


def key_entries() -> list[dict[str, Any]]:
    """alice (a writer with `record:security`), bob (a writer without it), carol (no record scope),
    dave (a writer of another team), a watcher with `record:security`, a guard with
    `release:query` and a `service` reader with `record:read`, each for the repository REPO."""
    return [
        _agent(ALICE, TEAM, ["record:read", "record:write", "record:security", "device:controller"]),
        _agent(BOB, TEAM, ["record:read", "record:write", "device:controller"]),
        _agent(CAROL, TEAM, ["device:executor"]),
        _agent(DAVE, OTHER_TEAM, ["record:read", "record:write", "record:security", "device:controller"]),
        _service(WATCHER, "example-watcher", ["record:read", "record:watch", "record:security"], [REPO]),
        _service(GUARD, "example-guard", ["release:query"], [REPO]),
        _service(READER, "example-reader", ["record:read"], [REPO]),
    ]


def write_files(directory: Path, entries: list[dict[str, Any]]) -> tuple[Path, Path]:
    teams = directory / "hub-teams.yaml"
    teams.write_text(
        yaml.safe_dump({"teams": {TEAM: {"members": ["alice", "bob", "carol"]}, OTHER_TEAM: {"members": ["dave"]}}}),
        encoding="utf-8",
    )
    keys = directory / "hub-keys.yaml"
    keys.write_text(yaml.safe_dump({"keys": entries}, sort_keys=False), encoding="utf-8")
    return keys, teams


class Monotonic:
    """A fixed monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Points:
    """The test points of a hub; each does nothing until a test sets it."""

    in_read_fold: Callable[[], None] = lambda: None
    append_before_insert: Callable[[], None] = lambda: None
    append_after_commit: Callable[[], None] = lambda: None
    snapshot_between_reads: Callable[[], None] = lambda: None
    checkpoint_before_insert: Callable[[], None] = lambda: None

    def hooks(self) -> Hooks:
        return Hooks(
            in_read_fold=lambda: self.in_read_fold(),
            append_before_insert=lambda: self.append_before_insert(),
            append_after_commit=lambda: self.append_after_commit(),
            snapshot_between_reads=lambda: self.snapshot_between_reads(),
            checkpoint_before_insert=lambda: self.checkpoint_before_insert(),
        )


@dataclass
class Hub:
    """One hub process on a test database."""

    app: FastAPI
    clock: FixedClock
    monotonic: Monotonic
    points: Points
    store: Store
    seeds: dict[str, int]
    prev: dict[str, str] = field(default_factory=dict)

    @property
    def runtime(self) -> RecordRuntime:
        return self.app.state.record

    def close(self) -> None:
        self.runtime.stop()
        self.store.close()

    def send(self, wire: Wire) -> Answer:
        return send(self.app, wire)

    def wire(self, key_id: str, target: str, *, method: str = "GET", body: bytes = b"", **options: Any) -> Wire:
        """A request of `key_id` signed with its seed at the hub clock, unless `options` names
        another seed or time."""
        options.setdefault("seed", self.seeds[key_id])
        options.setdefault("time_text", fmt(self.clock.now))
        return build_wire(key_id, method=method, target=target, body=body, **options)

    def get(self, key_id: str, target: str, **options: Any) -> tuple[Wire, Answer]:
        wire = self.wire(key_id, target, **options)
        return wire, self.send(wire)

    def signed(self, key_id: str, target: str, status: int, **options: Any) -> Answer:
        """A request whose answer must carry the hub signature and the given status."""
        wire, answer = self.get(key_id, target, **options)
        assert_signed(wire, answer, status, self.clock.now)
        return answer

    def event(
        self,
        key_id: str,
        object_type: str,
        object_id: str,
        changes: Sequence[tuple[str, str | None, str | None]],
        *,
        signed_at: int | None = None,
        prev_hash: str | None = None,
        event_id: str | None = None,
        certificate: bytes | None = None,
    ) -> tuple[bytes, bytes]:
        """A signed event of `key_id`, chained to the last event of that key this helper saw
        accepted, unless `prev_hash` is given."""
        text = build_event_text(
            event_id=event_id or new_id(),
            object_type=object_type,
            object_id=object_id,
            actor_key_id=key_id,
            signed_at=self.clock.now if signed_at is None else signed_at,
            prev_hash=self.prev.get(key_id, "-") if prev_hash is None else prev_hash,
            changes=[Change(name, encode(old), encode(new)) for name, old, new in changes],
            key_certificate=certificate,
        )
        return text, private(self.seeds[key_id]).sign(text)

    def post(self, key_id: str, path: str, text: bytes, signature: bytes, **options: Any) -> tuple[Wire, Answer]:
        options.setdefault("extra_headers", JSON)
        wire = self.wire(key_id, path, method="POST", body=append_body(text, signature), **options)
        answer = self.send(wire)
        if answer.status == 201:
            self.prev[key_id] = answer.json()["event_hash"]
        return wire, answer

    def write(
        self,
        key_id: str,
        object_type: str,
        object_id: str,
        changes: Sequence[tuple[str, str | None, str | None]],
        path: str = "/api/key/record/events",
    ) -> dict[str, Any]:
        """An event that the hub must accept (`201`); its answer."""
        wire, answer = self.post(key_id, path, *self.event(key_id, object_type, object_id, changes))
        assert_signed(wire, answer, 201, self.clock.now)
        return answer.json()

    def refused(
        self,
        key_id: str,
        object_type: str,
        object_id: str,
        changes: Sequence[tuple[str, str | None, str | None]],
        path: str = "/api/key/record/events",
    ) -> tuple[int, bytes]:
        wire, answer = self.post(key_id, path, *self.event(key_id, object_type, object_id, changes))
        assert_signed(wire, answer, answer.status, self.clock.now)
        return answer.status, answer.body


def new_id() -> str:
    return str(uuid.uuid4())


def encode(value: str | None) -> bytes | None:
    return None if value is None else value.encode("utf-8")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def append_body(text: bytes, signature: bytes) -> bytes:
    return json.dumps({"event": b64(text), "signature": b64(signature)}, separators=(",", ":")).encode("ascii")


def error(value: str) -> bytes:
    return json.dumps({"error": value}, separators=(",", ":")).encode("ascii")


def task_create(title: str = "Fix login", team: str = TEAM) -> list[tuple[str, str | None, str | None]]:
    return [("kind", None, "work"), ("team", None, team), ("title", None, title)]


def security_create(short_name: str, repository: str = REPO) -> list[tuple[str, str | None, str | None]]:
    return [
        ("kind", None, "security"),
        ("repository", None, repository),
        ("severity", None, "high"),
        ("short_name", None, short_name),
        ("team", None, TEAM),
        ("title", None, "Login bypass"),
        ("urgency", None, "8"),
    ]


def start_hub(
    db: RecordDatabase,
    cfg: Config,
    directory: Path,
    *,
    entries: list[dict[str, Any]] | None = None,
    keys_and_teams: tuple[Path, Path] | None = None,
    seeds: dict[str, int] | None = None,
    environ: dict[str, str] | None = None,
    clock: FixedClock | None = None,
    pool_size: int = 3,
    name: str = "hub",
    cfg_changes: dict[str, Any] | None = None,
    points: Points | None = None,
    poll_seconds: float | None = None,
) -> Hub:
    """Start a hub on `db`, which `migrate` has already prepared. Raises what the start raises."""
    if keys_and_teams is None:
        keys_and_teams = write_files(directory, key_entries() if entries is None else entries)
    keys, teams = keys_and_teams
    clock = FixedClock(NOW) if clock is None else clock
    monotonic = Monotonic()
    points = Points() if points is None else points
    app_cfg = replace(
        cfg,
        public_url="http://testserver",
        teams_path=teams,
        keys_path=keys,
        database=str(directory / f"{name}.sqlite"),
        **(cfg_changes or {}),
    )
    starter = partial(
        start_record,
        {"AGENT_CORE_RECORD_DATABASE": db.writer_url} if environ is None else environ,
        monotonic=monotonic,
        pool_size=pool_size,
        poll_seconds=poll_seconds,
        hooks=points.hooks(),
    )
    store = Store(app_cfg.database)
    try:
        app = create_app(app_cfg, github=FakeGitHub({"code-alice": "alice"}), store=store, clock=clock, record=starter)
    except BaseException:
        store.close()
        raise
    return Hub(app=app, clock=clock, monotonic=monotonic, points=points, store=store, seeds=dict(seeds or SEEDS))
