"""Test support of the outbox suites (U-HUB-outbox): items of every kind and pushes signed by the device's own key.

`Outbox` wraps the tasks desk (`tests/platform/hub/tasks/support.py`: an org with users, the org devices `device` and
`other_device` assigned to the workgroup `wg`, both online) and the fleet (clients that sign with a device's key, so
the device resolver of HUB-08 checks them). Item builders return contract-shaped items; `push` advances the hub clock
by 300 ms first, so the push pacer (three pushes' worth of refill) never interferes unless a test wants it to.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx

from agent_hub.crypto.encoding import format_time
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.events.support import Json, Scene
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.tasks.support import Desk

NOT_CONTRACT = {"contract": False}
DELTA = {
    "requests": 1,
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_tokens": 5,
    "cache_write_tokens": 3,
    "thinking_tokens": 7,
    "cost_micro_usd": 1500,
}


def item(origin_seq: int, item_kind: str, body: Json) -> Json:
    return {"origin_seq": origin_seq, "item_kind": item_kind, "body": body}


def usage_body(session: str, **delta: int) -> Json:
    return {
        "session_id": session,
        "delta": {**DELTA, **delta},
        "cost_basis": "provider",
        "context_used_tokens": 1000,
        "context_window_tokens": 200000,
        "last_provider": "deepseek",
    }


def sample(entity_kind: str, entity_id: str, ts: str, **values: Any) -> Json:
    found: Json = {
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "ts": ts,
        "cpu_ratio": 0.5,
        "mem_used_bytes": 1024,
        "mem_limit_bytes": 4096,
        "disk_used_bytes": 10,
        "disk_limit_bytes": 100,
        "net_rx_bytes": 7,
        "net_tx_bytes": 8,
        "uptime_seconds": 60,
        "reachable": True,
        "state": "running",
        "free_vm_slots": None,
        "free_mem_bytes": None,
    }
    found.update(values)
    return found


class Outbox:
    """Pushes of the scene's devices (module docstring)."""

    def __init__(self, desk: Desk, fleet: Fleet) -> None:
        self.desk = desk
        self.fleet = fleet
        # bind the scene's devices to its org, as an org enrollment stores them, so they pass the device resolver
        desk.execute(
            "UPDATE hub.device SET owner_org = %s WHERE id = ANY(%s::uuid[])",
            desk.org,
            [desk.scene.device.id, desk.scene.other_device.id],
        )

    @property
    def scene(self) -> Scene:
        return self.desk.scene

    @property
    def hub(self) -> Any:
        return self.desk.hub

    @property
    def device(self) -> Actor:
        return self.scene.device

    def now(self, seconds: float = 0) -> str:
        return format_time(self.hub.clock() + timedelta(seconds=seconds))

    def push(self, *items: Json, device: Actor | None = None, pace: bool = True, **options: Any) -> httpx.Response:
        if pace:
            self.hub.clock.advance(0.3)
        return self.fleet.client(device or self.device).post(
            "/v1/device/outbox", json={"items": list(items)}, **options
        )

    def ok(self, *items: Json, device: Actor | None = None) -> Json:
        """A push that must succeed; its body."""
        response = self.push(*items, device=device)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def statuses(self, *items: Json, device: Actor | None = None) -> list[tuple[int, str, str | None]]:
        return [(r["origin_seq"], r["status"], r.get("error")) for r in self.ok(*items, device=device)["results"]]

    # --- state ---

    def position(self, device: Actor | None = None) -> int:
        rows = self.desk.rows(
            "SELECT last_origin_seq FROM hub.device_stream WHERE device_id = %s", (device or self.device).id
        )
        return int(rows[0][0]) if rows else 0

    def receipts(self, device: Actor | None = None) -> list[tuple[int, str, str, str | None]]:
        return [
            (int(row[0]), str(row[1]), str(row[2]), row[3])
            for row in self.desk.rows(
                "SELECT origin_seq, item_kind, status, error FROM hub.outbox_receipt WHERE device_id = %s"
                " ORDER BY origin_seq",
                (device or self.device).id,
            )
        ]

    def rejected_audit(self) -> list[Json]:
        return [
            dict(row[0])
            for row in self.desk.rows("SELECT detail FROM hub.audit WHERE action = 'outbox.rejected' ORDER BY seq")
        ]

    # --- item bodies ---

    def session(self, device: Actor | None = None) -> tuple[str, str]:
        """A task and a `working` session on the device, known to the scene (E7 for its session events)."""
        return self.scene.start_session(device or self.device)

    def usage(self, origin_seq: int, session: str, **delta: int) -> Json:
        return item(origin_seq, "usage", usage_body(session, **delta))

    def transition(self, origin_seq: int, session: str, state: str, reason: str | None = None, **extra: Any) -> Json:
        return item(
            origin_seq,
            "session_transition",
            {"session_id": session, "state": state, "reason": reason, "at": self.now(-0.04), **extra},
        )

    def vm(self, origin_seq: int, vm_id: str, state: str = "running", wg: str | None = "default", **extra: Any) -> Json:
        body = {
            "vm_id": vm_id,
            "wg_id": self.desk.wg if wg == "default" else wg,
            "state": state,
            "golden_version": "golden-1",
            "cpus": 4,
            "memory_mib": 8192,
            "disk_gib": 20,
            "at": self.now(-1),
        }
        body.update(extra)
        return item(origin_seq, "vm_state", body)

    def event(self, origin_seq: int, event: Json) -> Json:
        return item(origin_seq, "event", {"event": event})

    def output(self, origin_seq: int, session: str, device: Actor | None = None, **extra: Any) -> Json:
        return self.event(origin_seq, self.scene.session_event(device or self.device, session, **extra))

    def audit(self, origin_seq: int, action: str, detail: Json, **extra: Any) -> Json:
        body = {"action": action, "result": "ok", "target_kind": "session", "target_id": "x", "detail": detail}
        body.update(extra)
        return item(origin_seq, "audit", body)


def new_vm() -> str:
    return "vm-" + new_id().replace("-", "")[-8:]
