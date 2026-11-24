"""Test support of the usage suites (U-HUB-usage): usage and quota items, subscription credentials, the device state
and the platform spend of the scene's org.

`Meter` wraps the operations helper (`tests/platform/hub/operations/support.py`: the outbox scene - an org with
`admin`, `writer`, `reader`, `stranger` and the two online org devices `device` and `other_device` assigned to `wg` -
plus operation registration). `low`/`high` are the two devices ordered by ID: `low` polls the quotas while it is
online. The test hub has a platform budget of `BUDGET` USD per org and day (`conftest.py`).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

import httpx

from agent_hub.crypto.encoding import format_time
from tests.platform.hub.events.support import Json
from tests.platform.hub.operations.support import Ops
from tests.platform.hub.orgs.support import Actor, new_id
from tests.platform.hub.outbox.support import Outbox, item, usage_body

BUDGET = "2.5"
SUBAGENT = "subagent.request"


def window(name: str, used: float, resets: str | None = None) -> Json:
    return {"window": name, "used_percent": used, "resets_at": resets, "severity": None if used < 80 else "warning"}


class Meter:
    """Usage, quotas and budget of the scene (module docstring)."""

    def __init__(self, ops: Ops) -> None:
        self.ops = ops
        devices = sorted([ops.scene.device, ops.scene.other_device], key=lambda actor: actor.id)
        self.low, self.high = devices
        self.seqs: dict[str, int] = {}

    @property
    def outbox(self) -> Outbox:
        return self.ops.outbox

    @property
    def desk(self) -> Any:
        return self.ops.desk

    @property
    def scene(self) -> Any:
        return self.ops.scene

    @property
    def hub(self) -> Any:
        return self.ops.hub

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return list(self.desk.rows(query, *params))

    def execute(self, query: str, *params: object) -> None:
        self.desk.execute(query, *params)

    # --- outbox ---

    def next_seq(self, device: Actor) -> int:
        self.seqs[device.id] = self.seqs.get(device.id, 0) + 1
        return self.seqs[device.id]

    def push(self, kind: str, body: Json, device: Actor | None = None, **options: Any) -> httpx.Response:
        sender = device or self.outbox.device
        return self.outbox.push(item(self.next_seq(sender), kind, body), device=sender, **options)

    def status(self, kind: str, body: Json, device: Actor | None = None) -> tuple[str, str | None]:
        response = self.push(kind, body, device)
        assert response.status_code == 200, response.text
        result = response.json()["results"][0]
        return result["status"], result.get("error")

    def usage(self, session: str, device: Actor | None = None, **delta: int) -> tuple[str, str | None]:
        return self.status("usage", usage_body(session, **delta), device)

    def quota_body(self, owner: str, *windows: Json, fetched: str | None = None) -> Json:
        return {"owner_id": owner, "windows": list(windows), "fetched_at": fetched or self.outbox.now(-1)}

    def quota(self, owner: str, *windows: Json, device: Actor | None = None, fetched: str | None = None) -> Any:
        return self.status("quota", self.quota_body(owner, *windows, fetched=fetched), device or self.low)

    # --- scene ---

    def session(self, access: str | None = "platform", device: Actor | None = None) -> tuple[str, str]:
        """A task of `writer` and its `working` main session with the reported model access."""
        task, session = self.ops.session(device)
        self.execute("UPDATE hub.session SET model_access = %s WHERE id = %s", access, session)
        return task, session

    def subscribe(self, owner: Actor) -> None:
        """A stored `claude_subscription` credential of the owner (the hub keeps it as opaque ciphertext)."""
        self.execute(
            "INSERT INTO hub.credential (id, owner_id, kind, version, epoch, header_text, ct, sig, updated_at,"
            " created_at, is_primary) VALUES (%s, %s, 'claude_subscription', 1, 1, 'h', %s, %s, %s, %s, true)",
            new_id(),
            owner.id,
            secrets.token_bytes(32),
            secrets.token_bytes(64),
            self.hub.clock(),
            self.hub.clock(),
        )

    def platform_access(self, granted: bool = True) -> None:
        self.execute("UPDATE hub.org SET platform_access = %s WHERE id = %s", granted, self.desk.org)

    def state(self, device: Actor) -> Json:
        response = self.ops.fleet.client(device).get("/v1/device/state")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def poll_owners(self, device: Actor) -> list[str]:
        return list(self.state(device)["quota_poll_owners"])

    def spent(self, day: str | None = None) -> list[tuple[str, int]]:
        """`(day, micro-USD)` of the org, oldest day first."""
        rows = self.rows(
            "SELECT day::text, sum(cost_micro_usd) FROM hub.usage_daily WHERE org_id = %s GROUP BY day ORDER BY day",
            self.desk.org,
        )
        return [(str(row[0]), int(row[1])) for row in rows if day is None or row[0] == day]

    def until(self, moment: datetime) -> None:
        """Advance the hub clock to `moment`."""
        self.hub.clock.advance((moment - self.hub.clock()).total_seconds())

    def time(self, seconds: float = 0) -> str:
        return format_time(self.hub.clock() + timedelta(seconds=seconds))
