"""Test support of the tasks, sessions, scheduler and workgroups suites (U-HUB-tasks, U-HUB-scheduler).

`Desk` wraps the events scene (`tests/platform/hub/events/support.py`: an org with `admin`, `writer`, `reader`,
`stranger` and two org devices `device` and `other_device` assigned to the workgroup `wg`) and adds what the tasks
area needs: device presence and capacity rows (as the live channels of HUB-07 store them), VM rows (as the outbox
stores them), task creation and prompts through the browser API, and session transitions through
`agent_hub.sessions.transitions.apply` (as the outbox calls it), with the effects sent after the commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from psycopg.types.json import Jsonb

from agent_hub.sessions.transitions import Applied, apply
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.conftest import contracts_registry
from tests.platform.hub.events.support import Json, Scene
from tests.platform.hub.orgs.support import Actor, World, new_id

CAPACITY = {
    "vm_memory_budget_mib": 65536,
    "vm_memory_used_mib": 8192,
    "vm_slots_total": 8,
    "vm_slots_free": 7,
    "pool_ready": 2,
}
HINT_SCHEMAS = {
    "session.start": "ws/device/session.start.json",
    "session.control": "ws/device/session.control.json",
    "prompt": "ws/device/prompt.json",
}
FRAME_SCHEMAS = {"task": "ws/sse/task.json", "session": "ws/sse/session.json"}


@dataclass
class Desk:
    """The scene plus helpers of the tasks suites (module docstring)."""

    scene: Scene

    @classmethod
    def build(cls, world: World) -> Desk:
        return cls(Scene.build(world))

    @property
    def hub(self) -> Any:
        return self.scene.world.hub

    @property
    def wg(self) -> str:
        return self.scene.wg

    @property
    def org(self) -> str:
        return self.scene.org

    # --- rows ---

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.scene.world.rows(query, *params)

    def execute(self, query: str, *params: object) -> None:
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(query, params)

    def online(self, device: Actor, capacity: Mapping[str, int] | None = None, *, seen: datetime | None = None) -> None:
        """A presence row with a fresh heartbeat (or `seen`) and the reported capacity."""
        now = self.hub.clock()
        self.execute(
            "INSERT INTO hub.device_presence (device_id, hub_instance, connected_at, last_heartbeat_at)"
            " VALUES (%s, 'test', %s, %s) ON CONFLICT (device_id) DO UPDATE SET last_heartbeat_at = EXCLUDED"
            ".last_heartbeat_at",
            device.id,
            now,
            seen or now,
        )
        self.set_capacity(device, capacity or CAPACITY)

    def offline(self, device: Actor) -> None:
        self.execute("DELETE FROM hub.device_presence WHERE device_id = %s", device.id)

    def set_capacity(self, device: Actor, capacity: Mapping[str, int]) -> None:
        self.execute("UPDATE hub.device SET capacity = %s WHERE id = %s", Jsonb(dict(capacity)), device.id)

    def vm(self, device: Actor, state: str, wg: str | None = None) -> str:
        """A VM row of the device for the workgroup."""
        vm_id = "vm-" + new_id().replace("-", "")[-8:]
        now = self.hub.clock()
        self.execute(
            "INSERT INTO hub.vm (id, device_id, wg_id, state, golden_version, cpus, memory_mib, disk_gib, created_at,"
            " state_at) VALUES (%s, %s, %s, %s, 'golden-1', 4, 8192, 20, %s, %s)",
            vm_id,
            device.id,
            wg or self.wg,
            state,
            now,
            now,
        )
        return vm_id

    # --- browser API ---

    def task_events(self, actor: Actor, task: str, wg: str | None = None) -> list[Json]:
        scope = wg or self.wg
        meta = self.scene.wg_event(actor, "task.meta", task=task, scope_id=scope, wg=scope)
        prompt = self.scene.wg_event(actor, "prompt", task=task, scope_id=scope, wg=scope)
        return [meta, prompt]

    def create(
        self,
        actor: Actor,
        task: str | None = None,
        *,
        wg: str | None = None,
        events: list[Json] | None = None,
        **options: Any,
    ) -> httpx.Response:
        task_id = task or new_id()
        body = {"task_id": task_id, "wg_id": wg or self.wg, "events": events or self.task_events(actor, task_id, wg)}
        return actor.browser.post("/v1/tasks", json=body, **options)

    def started(self, actor: Actor | None = None) -> tuple[str, str]:
        """A created task and its main session (the caller made a device available)."""
        response = self.create(actor or self.scene.writer)
        assert response.status_code == 200, response.text
        body = response.json()
        return body["task"]["task_id"], body["session"]["session_id"]

    def prompt_event(self, actor: Actor, task: str) -> Json:
        return self.scene.prompt(actor, task)

    def prompt(self, actor: Actor, task: str, event: Json | None = None, **options: Any) -> httpx.Response:
        return actor.browser.post(
            f"/v1/tasks/{task}/prompts", json={"event": event or self.prompt_event(actor, task)}, **options
        )

    # --- runner side ---

    def transition(
        self,
        session: str,
        state: str,
        reason: str | None = None,
        *,
        device: Actor | None = None,
        at: datetime | None = None,
        **details: str | None,
    ) -> Applied:
        """A transition as the outbox applies it (reported by `device`, default the scene's device)."""
        reporter = device or self.scene.device
        now = self.hub.clock()
        with self.hub.pool.connection() as connection, connection.transaction():
            applied = apply(
                connection,
                session,
                state,
                reason,
                at or now - timedelta(milliseconds=40),
                received_at=now,
                reported_by=reporter.id,
                **details,
            )
        applied.effects.flush()
        return applied

    def walk(self, session: str, *states: str, device: Actor | None = None) -> None:
        for state in states:
            self.transition(session, state, device=device)

    def session(self, session: str) -> Json:
        response = self.scene.writer.browser.get(f"/v1/sessions/{session}")
        assert response.status_code == 200, response.text
        return dict(response.json()["session"])

    def task(self, task: str) -> Json:
        response = self.scene.writer.browser.get(f"/v1/tasks/{task}")
        assert response.status_code == 200, response.text
        return dict(response.json()["task"])


def hints(published: MemoryPublisher, device: Actor | None = None, kind: str | None = None) -> list[Json]:
    """The device hints sent (checked against their contracts), as `{"type", ...}` frames."""
    found = []
    for item in published.items():
        if item.audience.kind != "device" or item.frame not in HINT_SCHEMAS:
            continue
        frame = {"type": item.frame, **item.data}
        contracts_registry().validate(HINT_SCHEMAS[item.frame], frame)
        if (device is None or str(item.audience.id) == device.id) and (kind is None or item.frame == kind):
            found.append(frame)
    return found


def frames(published: MemoryPublisher, frame: str) -> list[Json]:
    """The browser frames of one type (checked against their contracts)."""
    found = []
    for item in published.items():
        if item.audience.kind == "org" and item.frame == frame:
            contracts_registry().validate(FRAME_SCHEMAS[frame], dict(item.data))
            found.append(dict(item.data))
    return found


def audit_rows(desk: Desk, action: str) -> list[tuple[Any, ...]]:
    """`(actor_kind, actor_id, target_id, detail)` of the audit rows of an action, checked against the detail
    contract."""
    found = desk.rows(
        "SELECT actor_kind, actor_id, target_id, detail FROM hub.audit WHERE action = %s ORDER BY seq", action
    )
    for row in found:
        contracts_registry().validate(f"audit/{action}.json", row[3])
    return found
