"""Test support of the events suites: one org with every kind of author and helpers to post and read events.

`Scene.build` creates, on the running hub, a public org whose founder `admin` is its admin, a member `writer` with
write access and a member `reader` with read access to the workgroup `wg`, an org device `device` assigned to `wg`,
a second org device `other_device` (assigned to `wg` as well) and a `stranger` (a registered user of another org).
`task`/`session` rows (the tasks area's tables) let device-authored session events pass E7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import psycopg

from agent_hub.events.store import StoredEvent, append_events
from tests.platform.hub.orgs.support import Actor, OrgDraft, World, new_id

Json = dict[str, Any]


@dataclass
class Scene:
    """The actors and IDs of one org (module docstring)."""

    world: World
    draft: OrgDraft
    admin: Actor
    writer: Actor
    reader: Actor
    device: Actor
    other_device: Actor
    stranger: Actor
    wg: str
    sessions: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def build(cls, world: World) -> Scene:
        draft = world.public_org()
        admin = draft.founder
        writer, reader, device, other_device = world.user(), world.user(), world.device(), world.device()
        world.join(draft, writer)
        world.join(draft, reader)
        wg = new_id()
        draft.add("wg_create", admin, wg=wg, epoch=1)
        draft.add("grant", admin, subject=writer.id, wg=wg, access="write")
        draft.add("grant", admin, subject=reader.id, wg=wg, access="read")
        response = draft.post(admin, draft.wraps(admin, "wg", wg, 1, [admin, writer, reader]))
        assert response.status_code == 200, response.text
        world.join(draft, device, wgs={wg: "device"})
        world.join(draft, other_device, wgs={wg: "device"})
        stranger = world.public_org().founder
        return cls(world, draft, admin, writer, reader, device, other_device, stranger, wg)

    @property
    def org(self) -> str:
        return self.draft.id

    @property
    def lh(self) -> int:
        return len(self.draft.entries) - 1

    def org_event(self, author: Actor, kind: str = "org.profile", **extra: Any) -> Json:
        return self.draft.event(author, kind, **extra)

    def wg_event(self, author: Actor, kind: str = "settings.workgroup", **extra: Any) -> Json:
        extra.setdefault("wg", self.wg)
        return self.draft.event(author, kind, scope="wg", scope_id=extra.pop("scope_id", self.wg), **extra)

    def prompt(self, author: Actor, task: str, **extra: Any) -> Json:
        return self.wg_event(author, "prompt", task=task, **extra)

    def session_event(self, device: Actor, session: str, kind: str = "session.output", **extra: Any) -> Json:
        task = self.sessions[session][0] if session in self.sessions else new_id()
        return self.wg_event(device, kind, task=task, session=session, **extra)

    def start_session(
        self, device: Actor | None = None, *, wg: str | None = None, placed: bool = True
    ) -> tuple[str, str]:
        """A task and a session on `device` (default: the scene's device; none when not `placed`), as the tasks area
        stores them."""
        task, session = new_id(), new_id()
        now = self.world.hub.clock()
        target = wg or self.wg
        with self.world.hub.pool.connection() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
                " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
                (task, self.org, target, self.writer.id, now, now, now),
            )
            connection.execute(
                "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, depth, state, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, 0, 'working', %s)",
                (session, task, self.org, target, self.writer.id, (device or self.device).id if placed else None, now),
            )
        self.sessions[session] = (task, target)
        return task, session

    def post(self, actor: Actor, *events: Json, notify: list[bool] | None = None, **options: Any) -> httpx.Response:
        items: list[Json] = []
        for index, event in enumerate(events):
            item: Json = {"event": event}
            if notify is not None:
                item["notify"] = notify[index]
            items.append(item)
        return actor.browser.post("/v1/events", json={"events": items}, **options)

    def append(
        self, actor: Actor, *events: Json, now: datetime | None = None, origin_seqs: list[int] | None = None
    ) -> list[StoredEvent]:
        """`append_events` in its own committed transaction (devices and direct service calls)."""
        hub = self.world.hub
        with hub.pool.connection() as connection, connection.transaction():
            return append_events(
                connection,
                list(events),
                actor.id,
                now=now or hub.clock(),
                root_key=hub.config.platform_root_key,
                origin_seqs=origin_seqs,
            )

    def read(self, actor: Actor, scope_id: str | None = None, **query: str) -> httpx.Response:
        return actor.browser.get(f"/v1/scopes/{scope_id or self.wg}/events", params=query)

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.world.rows(query, *params)


def connect(url: str) -> psycopg.Connection[Any]:
    """An autocommit connection (LISTEN)."""
    return psycopg.connect(url, autocommit=True)
