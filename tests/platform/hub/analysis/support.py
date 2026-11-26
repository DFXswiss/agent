"""Test support of the analysis read suites (U-HUB-analysis): rows of one workgroup as the hub stores them.

`Reads` wraps the events scene with its devices bound to the scene's org (`org_device_scene`; `device` and
`other_device` are assigned to `wg`, the default workgroup has no device) and the fleet, whose clients sign as a
device. It writes tasks, sessions, usage counters, charges, operations and audit rows at chosen times before the
fixed hub clock and reads the four device routes `GET /v1/device/workgroups/{wg}/usage|audit|operations|metrics`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from agent_hub.audit.chain import AuditRow, append_audit
from tests.platform.contracts.audit_details import valid_detail
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.events.support import Scene
from tests.platform.hub.orgs.support import Actor, World, new_id

NOT_CONTRACT = {"contract": False}


@dataclass
class Reads:
    """The scene, its fleet and the row writers (module docstring)."""

    scene: Scene
    fleet: Fleet

    @classmethod
    def build(cls, world: World, fleet: Fleet) -> Reads:
        return cls(org_device_scene(world), fleet)

    @property
    def hub(self) -> Any:
        return self.scene.world.hub

    @property
    def wg(self) -> str:
        return self.scene.wg

    @property
    def other_wg(self) -> str:
        """The default workgroup of the org: no device is assigned to it."""
        return self.scene.draft.default_wg

    def now(self) -> datetime:
        return self.hub.clock()  # type: ignore[no-any-return]

    def ago(self, **delta: float) -> datetime:
        return self.now() - timedelta(**delta)

    def execute(self, query: str, *params: object) -> None:
        with self.hub.pool.connection() as connection, connection.transaction():
            connection.execute(query, params)

    # --- rows ---

    def task(self, at: datetime, wg: str | None = None) -> str:
        task = new_id()
        self.execute(
            "INSERT INTO hub.task (id, org_id, wg_id, owner_id, state, created_at, state_at, last_activity_at)"
            " VALUES (%s, %s, %s, %s, 'working', %s, %s, %s)",
            task,
            self.scene.org,
            wg or self.wg,
            self.scene.writer.id,
            at,
            at,
            at,
        )
        return task

    def session(
        self,
        task: str,
        at: datetime,
        *,
        parent: str | None = None,
        state: str = "working",
        ended: datetime | None = None,
        model: str | None = None,
        wg: str | None = None,
    ) -> str:
        session = new_id()
        self.execute(
            "INSERT INTO hub.session (id, task_id, org_id, wg_id, owner_id, device_id, parent_session_id, depth, state,"
            " created_at, ended_at, model_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            session,
            task,
            self.scene.org,
            wg or self.wg,
            self.scene.writer.id,
            self.scene.device.id,
            parent,
            0 if parent is None else 1,
            state,
            at,
            ended,
            model,
        )
        return session

    def usage(self, session: str, requests: int, tokens: int, cost: int) -> None:
        self.execute(
            "INSERT INTO hub.usage_counter (session_id, requests, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, thinking_tokens, cost_micro_usd, cost_basis, context_used_tokens,"
            " context_window_tokens, last_provider, updated_at) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, 'provider', 0,"
            " 200000, 'anthropic', %s)",
            session,
            requests,
            tokens,
            tokens // 2,
            tokens // 4,
            tokens // 8,
            cost,
            self.now(),
        )

    def charge(self, session: str, amount: int, origin: int) -> None:
        self.execute(
            "INSERT INTO hub.charge (id, org_id, wg_id, task_id, session_id, device_id, origin_seq, kind, at,"
            " amount_micro_usd, price_version, quantity) SELECT %s, org_id, wg_id, task_id, id, %s, %s, 'tokens', %s,"
            " %s, 1, '{}' FROM hub.session WHERE id = %s",
            new_id(),
            self.scene.device.id,
            origin,
            self.now(),
            amount,
            session,
        )

    def operation(
        self,
        session: str,
        at: datetime,
        *,
        op_type: str = "github.push",
        state: str = "succeeded",
        error: str | None = None,
        finished: datetime | None = None,
    ) -> str:
        operation = new_id()
        self.execute(
            "INSERT INTO hub.operation (id, org_id, wg_id, task_id, session_id, device_id, actor_id, type, state,"
            " error_code, created_at, finished_at) SELECT %s, org_id, wg_id, task_id, id, device_id, owner_id, %s, %s,"
            " %s, %s, %s FROM hub.session WHERE id = %s",
            operation,
            op_type,
            state,
            error,
            at,
            finished,
            session,
        )
        return operation

    def audit(
        self,
        action: str,
        at: datetime,
        *,
        wg: str | None = None,
        result: str = "ok",
        task: str | None = None,
        session: str | None = None,
        actor: Actor | None = None,
    ) -> int:
        """One audit row of the org (of the workgroup `wg`, the scene's when None; "" for none); its `seq`."""
        who = actor or self.scene.writer
        place = self.wg if wg is None else wg
        with self.hub.pool.connection() as connection:
            stored = append_audit(
                connection,
                AuditRow(
                    at=at,
                    actor_kind=who.kind,
                    actor_id=UUID(who.id),
                    action=action,
                    result=result,
                    org_id=UUID(self.scene.org),
                    wg_id=UUID(place) if place else None,
                    task_id=None if task is None else UUID(task),
                    session_id=None if session is None else UUID(session),
                    target_kind="test",
                    target_id=action,
                    detail=valid_detail(action),
                ),
            )
        return stored.seq

    # --- reads ---

    def get(self, resource: str, wg: str | None = None, device: Actor | None = None, **query: Any) -> httpx.Response:
        client = self.fleet.client(device or self.scene.device)
        return client.get(f"/v1/device/workgroups/{wg or self.wg}/{resource}", params=query)

    def ok(self, resource: str, **query: Any) -> dict[str, Any]:
        response = self.get(resource, **query)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def refused(self, resource: str, wg: str | None = None, **query: Any) -> tuple[int, dict[str, Any]]:
        client = self.fleet.client(self.scene.device)
        response = client.get(
            f"/v1/device/workgroups/{wg or self.wg}/{resource}", params=query, extensions=NOT_CONTRACT
        )
        return response.status_code, dict(response.json())


def invalid(pointer: str) -> tuple[int, dict[str, Any]]:
    return 400, {"error": "request_invalid", "detail": {"pointers": [pointer]}}


NOT_FOUND = (404, {"error": "not_found"})
