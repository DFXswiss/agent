"""Test support of the whiteboard and notification suites: whiteboard events on the three levels of one scene.

`Board` wraps the events scene (`tests.platform.hub.events.support.Scene`: an org with an admin, a writer and a reader
of the workgroup `wg`, two assigned devices and a stranger) and adds `outsider`, a member of the org without access to
`wg`, and `other_wg`, a second workgroup the writer writes and the reader cannot read; both devices are owned by the
org (they sign device requests).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, new_id

Json = dict[str, Any]


@dataclass
class Board:
    """Whiteboard events of one scene (module docstring)."""

    scene: Scene
    outsider: Actor
    other_wg: str

    @classmethod
    def build(cls, scene: Scene) -> Board:
        world, draft = scene.world, scene.draft
        outsider = world.user()
        world.join(draft, outsider)
        other_wg = new_id()
        draft.add("wg_create", scene.admin, wg=other_wg, epoch=1)
        draft.add("grant", scene.admin, subject=scene.writer.id, wg=other_wg, access="write")
        response = draft.post(scene.admin, draft.wraps(scene.admin, "wg", other_wg, 1, [scene.admin, scene.writer]))
        assert response.status_code == 200, response.text
        with world.hub.pool.connection() as connection, connection.transaction():  # the devices sign as org devices
            connection.execute(
                "UPDATE hub.device SET owner_org = %s WHERE id = ANY(%s::uuid[])",
                (scene.org, [scene.device.id, scene.other_device.id]),
            )
        return cls(scene, outsider, other_wg)

    def event(
        self, author: Actor, kind: str, ref: str, *, level: str = "workgroup", task: str = "", wg: str | None = None
    ) -> Json:
        """A whiteboard event of `kind` for the entry `ref` on `level` (task level: `task`)."""
        if level == "org":
            return self.scene.org_event(author, kind, ref=ref)
        target = wg or self.scene.wg
        return self.scene.wg_event(author, kind, ref=ref, task=task, wg=target, scope_id=target)

    def entry(self, author: Actor, *, level: str = "workgroup", task: str = "", wg: str | None = None) -> Json:
        """A new entry (fresh ID)."""
        return self.event(author, "whiteboard.entry", new_id(), level=level, task=task, wg=wg)

    def post(self, author: Actor, *events: Json, notify: list[bool] | None = None) -> httpx.Response:
        return self.scene.post(author, *events, notify=notify)

    def posted(self, author: Actor, *events: Json, notify: list[bool] | None = None) -> list[Json]:
        """Post and require success; the events."""
        response = self.post(author, *events, notify=notify)
        assert response.status_code == 200, response.text
        return list(events)

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.scene.rows(query, *params)


def ref_of(event: Json) -> str:
    return str(event["header"]["ref"])


def id_of(event: Json) -> str:
    return str(event["header"]["id"])
