"""Test support of the models suites (U-HUB-models, KOR-50): per-device DFX keys, quotas and the model fallback.

`Keys` wraps the billing helper (`tests/platform/hub/billing/support.py`): the scene's org device `device` holds keys,
the scene's other device becomes the platform device `issuer` (`issuer_ready`: platform, online, announced). The
helpers push the items of ADR-080 as the runner sends them (checked against the contracts by the test client) and run
the job `models.dfx_keys`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from agent_hub.crypto.encoding import b64u_encode, format_time
from agent_hub.models import jobs
from tests.platform.hub.billing.support import Bill
from tests.platform.hub.events.support import Json
from tests.platform.hub.orgs.support import Actor

ENVELOPE = b64u_encode(b'{"v":1,"header":{},"ct":"sealed","sig":"signed"}')


class Keys:
    """Per-device keys of the scene (module docstring)."""

    def __init__(self, bill: Bill) -> None:
        self.bill = bill

    @property
    def device(self) -> Actor:
        return self.bill.device

    @property
    def issuer(self) -> Actor:
        found: Actor = self.bill.scene.other_device
        return found

    @property
    def org(self) -> str:
        return self.bill.org

    @property
    def hub(self) -> Any:
        return self.bill.hub

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.bill.rows(query, *params)

    def execute(self, query: str, *params: object) -> None:
        self.bill.execute(query, *params)

    def online(self, device: Actor) -> None:
        self.bill.meter.desk.online(device)

    def status(self, kind: str, body: Json, device: Actor | None = None) -> tuple[str, str | None]:
        return self.bill.meter.status(kind, body, device or self.issuer)

    def raw_status(self, kind: str, body: Json, device: Actor | None = None) -> tuple[str, str | None]:
        """A push the contract refuses (the client does not check it)."""
        response = self.bill.meter.push(kind, body, device or self.issuer, extensions={"contract": False})
        assert response.status_code == 200, response.text
        result = response.json()["results"][0]
        return result["status"], result.get("error")

    def issuer_ready(self) -> Actor:
        """The other device as an online platform device that announced a management key."""
        self.bill.platform(self.issuer)
        self.online(self.issuer)
        assert self.status("dfx_key_state", {"issuer": True}) == ("ok", None)
        return self.issuer

    def state(self, device: Actor | None = None) -> Json:
        self.online(device or self.issuer)
        return self.bill.meter.state(device or self.issuer)

    def plan(self) -> list[UUID]:
        return jobs.keys_pass(self.hub)

    def requests(self) -> list[Json]:
        return list(self.state()["dfx_keys"]["requests"])

    def listed(self) -> list[Json]:
        return list(self.state()["dfx_keys"]["keys"])

    def request_id(self, device: Actor | None = None) -> str:
        rows = self.rows(
            "SELECT request_id::text FROM hub.dfx_key WHERE device_id = %s AND revoked_at IS NULL",
            (device or self.device).id,
        )
        return str(rows[0][0])

    def issue(self, request_id: str, key_ref: str = "dfx-key-0001", **fields: Any) -> tuple[str, str | None]:
        body = {
            "request_id": request_id,
            "org_id": self.org,
            "device_id": self.device.id,
            "key_ref": key_ref,
            "envelope": ENVELOPE,
            **fields,
        }
        return self.status("dfx_key_issued", body)

    def issued(self, key_ref: str = "dfx-key-0001") -> str:
        """A planned and issued key of the org device; its request ID."""
        self.plan()
        request = self.request_id()
        assert self.issue(request, key_ref) == ("ok", None)
        return request

    def spend(
        self, spent: int, key_ref: str = "dfx-key-0001", at: datetime | None = None, **fields: Any
    ) -> tuple[str, str | None]:
        body = {
            "key_ref": key_ref,
            "org_id": self.org,
            "device_id": self.device.id,
            "spent_micro_usd": spent,
            "at": self.bill.meter.time(-1) if at is None else format_time(at),
            **fields,
        }
        return self.status("dfx_key_usage", body)

    def key(self, key_ref: str = "dfx-key-0001") -> tuple[Any, ...]:
        """`(envelope, booked_micro_usd, revoked_at, closed_at)` of a key."""
        return self.rows(
            "SELECT envelope, booked_micro_usd, revoked_at, closed_at FROM hub.dfx_key WHERE key_ref = %s", key_ref
        )[0]

    def charges(self) -> list[tuple[Any, ...]]:
        """`(session_id, part, amount_micro_usd, quantity)` of the key bookings, in part order."""
        return self.rows(
            "SELECT session_id::text, part, amount_micro_usd, quantity FROM hub.charge"
            " WHERE quantity->>'source' = 'provider_key' ORDER BY origin_seq, part"
        )

    def cost(self, session: str, micro_usd: int, device: Actor | None = None) -> tuple[str, str | None]:
        """A usage item of the org device's session on DFX access with the provider cost `micro_usd`."""
        return self.bill.usage(
            session, device=device or self.device, access_origin="platform", tokens={"cost_micro_usd": micro_usd}
        )
