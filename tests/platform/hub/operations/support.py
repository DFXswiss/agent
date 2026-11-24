"""Test support of the operations suites (U-HUB-operations): registrations signed by the device's own key.

`Ops` wraps the outbox helper (`tests/platform/hub/outbox/support.py`: the tasks desk with an org, the users `writer`
(task owner, write access), `reader`, `stranger`, the org devices `device` and `other_device` assigned to `wg`, both
online and bound to the org). `request` builds a contract-shaped registration with a real `operation.params` event
sealed by the device; `register` posts it (the test client validates request and response against the contracts).
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

import httpx

from agent_hub.crypto.encoding import b64u_encode, format_time
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.events.support import Json
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.outbox.support import Outbox, item

NOT_CONTRACT = {"contract": False}
NO_IDENTITY = {"github_identity": None, "triggered_by": None}
"""The identity fields of an `operation.*` audit detail of a registration without them (ADR-076 section 6)."""


def tag(name: str) -> str:
    """A dedupe tag (b64u of 32 bytes) derived from a name."""
    return b64u_encode(hashlib.sha256(name.encode()).digest())


class Ops:
    """Registrations and operation steps of the scene's devices (module docstring)."""

    def __init__(self, outbox: Outbox, fleet: Fleet) -> None:
        self.outbox = outbox
        self.fleet = fleet

    @property
    def desk(self) -> Any:
        return self.outbox.desk

    @property
    def scene(self) -> Any:
        return self.outbox.scene

    @property
    def hub(self) -> Any:
        return self.outbox.hub

    @property
    def device(self) -> Actor:
        return self.outbox.device

    def session(self, device: Actor | None = None) -> tuple[str, str]:
        """A task of `writer` and its `working` main session on the device."""
        return self.outbox.session(device)

    def request(
        self,
        session: str,
        op_type: str = "github.comment",
        /,
        *,
        dedupe: str | None = None,
        epoch: int = 1,
        local_check: str = "passed",
        error_code: str | None = None,
        operation: str | None = None,
        device: Actor | None = None,
        **header: Any,
    ) -> Json:
        task, wg = self.scene.sessions[session]
        operation_id = operation or new_id()
        fields = {"task": task, "session": session, "ref": operation_id, **header}
        event = self.scene.wg_event(device or self.device, "operation.params", epoch=epoch, **fields)
        body: Json = {
            "operation_id": operation_id,
            "session_id": session,
            "task_id": task,
            "wg_id": wg,
            "type": op_type,
            "requested_at": format_time(self.hub.clock() - timedelta(milliseconds=50)),
            "local_check": local_check,
            "dedupe": None if dedupe is None else {"epoch": epoch, "tag": tag(dedupe)},
            "params_event": event,
        }
        if error_code is not None:
            body["error_code"] = error_code
        return body

    def register(self, body: Json, device: Actor | None = None, **options: Any) -> httpx.Response:
        return self.fleet.client(device or self.device).post("/v1/device/operations", json=body, **options)

    def decide(self, session: str, op_type: str = "github.comment", **options: Any) -> tuple[str, Json]:
        """Register an operation that must be answered 200; its ID and the answer."""
        body = self.request(session, op_type, **options)
        response = self.register(body)
        assert response.status_code == 200, response.text
        return str(body["operation_id"]), dict(response.json())

    # --- rows ---

    def operation(self, operation: str) -> tuple[Any, ...]:
        rows = self.desk.rows(
            "SELECT state, error_code, duplicate_of::text, dedupe_epoch, dedupe_tag FROM hub.operation WHERE id = %s",
            operation,
        )
        return tuple(rows[0])

    def state(self, operation: str) -> str:
        return str(self.operation(operation)[0])

    def transitions(self, operation: str) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in self.desk.rows(
                "SELECT seq, state, error_code, by_id::text FROM hub.operation_transition WHERE operation_id = %s"
                " ORDER BY seq",
                operation,
            )
        ]

    def slot_holder(self, name: str, epoch: int = 1) -> str | None:
        rows = self.desk.rows(
            "SELECT operation_id::text FROM hub.dedupe_slot WHERE wg_id = %s AND epoch = %s AND tag = %s",
            self.desk.wg,
            epoch,
            tag(name),
        )
        return str(rows[0][0]) if rows else None

    def audit(self, operation: str) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in self.desk.rows(
                "SELECT action, result, actor_kind, actor_id::text, detail FROM hub.audit"
                " WHERE target_kind = 'operation' AND target_id = %s ORDER BY seq",
                operation,
            )
        ]

    # --- runner steps through the outbox ---

    def finish(self, operation: str, state: str = "succeeded", **extra: Any) -> str | None:
        """Report `executing` and a final state through the outbox (with an `operation.result` event); the result
        event ID for `succeeded`/`failed`."""
        seq = self.outbox.position() + 1
        items: list[Json] = [item(seq, "operation_transition", self._step(operation, "executing"))]
        result_id = None
        if state in ("succeeded", "failed"):
            event = self.scene.wg_event(self.device, "operation.result", ref=operation)
            result_id = str(event["header"]["id"])
            items.append(self.outbox.event(seq + 1, event))
            extra = {"result_event_id": result_id, **extra}
            if state == "failed":
                extra = {"error_code": "upstream_error", "retryable": True, **extra}
        else:
            extra = {"error_code": "timeout", **extra}
        items.append(item(seq + len(items), "operation_transition", self._step(operation, state, **extra)))
        statuses = self.outbox.statuses(*items)
        assert all(status == "ok" for _seq, status, _error in statuses), statuses
        return result_id

    def step(self, operation: str, state: str, **extra: Any) -> list[tuple[int, str, str | None]]:
        """One `operation_transition` item; the push's statuses."""
        seq = self.outbox.position() + 1
        return self.outbox.statuses(item(seq, "operation_transition", self._step(operation, state, **extra)))

    def _step(self, operation: str, state: str, **extra: Any) -> Json:
        return {"operation_id": operation, "state": state, "at": self.outbox.now(-0.01), **extra}
