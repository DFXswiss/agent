"""Test support of the billing suites (U-HUB-billing).

`Bill` wraps the usage helper (`tests/platform/hub/usage/support.py`: the outbox scene with the org devices `device`
and `other_device` assigned to `wg`, sessions, usage pushes). `platform(device)` turns a scene device into a platform
device (DFX hardware) the way the hub stores one (`kind = platform`, no owning org). `price_list` is a valid document;
`add_prices` stores it as a version.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from agent_hub.billing import jobs
from agent_hub.crypto.encoding import format_time
from tests.platform.hub.events.support import Json
from tests.platform.hub.orgs.support import Actor
from tests.platform.hub.outbox.support import usage_body
from tests.platform.hub.usage.support import Meter

PRICES: Json = {
    "currency": "USD",
    "valid_from": "2026-11-21T00:00:00Z",
    "tokens": {
        "factors": {"platform": 1.06, "operator": 1},
        "models": {
            "deepseek/deepseek-v4.1-flash-1017": {"input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0.3},
            "deepseek/deepseek-v4-pro": {"input": 1.32, "output": 3.96, "cache_read": 0.044, "cache_write": 1.32},
            "claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5},
            "claude-sonnet-5-5": {"input": 2, "output": 10, "cache_read": 0.1, "cache_write": 2.5},
            "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
            "claude-fable-5-2": {"input": 8, "output": 40, "cache_read": 0.2, "cache_write": 10},
        },
        "default": {"input": 8, "output": 40, "cache_read": 0.2, "cache_write": 10},
    },
    "runtime": {"per_hour": {"session": 0.36, "standard": 0.36, "large": 0.72, "xlarge": 1.44, "default": 1.44}},
    "note": "test",
}
FLASH = "deepseek/deepseek-v4.1-flash-1017"


def price_list(**changes: Any) -> Json:
    document = copy.deepcopy(PRICES)
    document.update(changes)
    return document


class Bill:
    """Prices, credit and charges of the scene (module docstring)."""

    def __init__(self, meter: Meter) -> None:
        self.meter = meter

    @property
    def org(self) -> str:
        return str(self.meter.desk.org)

    @property
    def hub(self) -> Any:
        return self.meter.hub

    @property
    def scene(self) -> Any:
        return self.meter.scene

    @property
    def device(self) -> Actor:
        return self.meter.outbox.device

    def rows(self, query: str, *params: object) -> list[tuple[Any, ...]]:
        return self.meter.rows(query, *params)

    def execute(self, query: str, *params: object) -> None:
        self.meter.execute(query, *params)

    def platform(self, device: Actor | None = None) -> Actor:
        found = device or self.device
        self.execute("UPDATE hub.device SET kind = 'platform', owner_org = NULL WHERE id = %s", found.id)
        return found

    def add_prices(self, document: Json | None = None, valid_from: datetime | None = None, version: int = 1) -> None:
        body = document or price_list()
        start = valid_from or datetime.fromisoformat(body["valid_from"])
        self.execute(
            "INSERT INTO hub.price_list (version, valid_from, document, created_at) VALUES (%s, %s, %s, %s)",
            version,
            start,
            Jsonb(body),
            self.hub.clock(),
        )

    def set_rate(self, day: str, chf_per_usd_e8: int, n: int | None = None, reason: str | None = None) -> None:
        """A USD/CHF rate row as `agent-hub billing fx set` writes it (without its audit row)."""
        number = n if n is not None else len(self.rows("SELECT n FROM hub.fx_rate")) + 1
        self.execute(
            "INSERT INTO hub.fx_rate (n, day, chf_per_usd_e8, source, set_at, actor, reason)"
            " VALUES (%s, %s, %s, 'BAZG', %s, 'operator', %s)",
            number,
            day,
            chf_per_usd_e8,
            self.hub.clock(),
            reason,
        )

    def settle(self) -> list[str]:
        return jobs.settle_pass(self.hub)

    def account(self, org: str | None = None) -> tuple[int, int] | None:
        found = self.rows(
            "SELECT balance_micro_usd, booked_micro_usd FROM hub.credit_account WHERE org_id = %s", org or self.org
        )
        return None if not found else (int(found[0][0]), int(found[0][1]))

    def entries(self, org: str | None = None) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT kind, amount_micro_usd, balance_micro_usd, reason, actor FROM hub.credit_entry WHERE org_id = %s"
            " ORDER BY n",
            org or self.org,
        )

    def charges(self) -> list[tuple[Any, ...]]:
        return self.rows(
            "SELECT kind, amount_micro_usd, price_version, quantity FROM hub.charge ORDER BY at, origin_seq"
        )

    def pending(self) -> int:
        return int(self.rows("SELECT COALESCE(sum(amount_micro_usd), 0) FROM hub.charge_pending")[0][0])

    def usage(
        self, session: str, *, device: Actor | None = None, tokens: Mapping[str, int] | None = None, **fields: Any
    ) -> tuple[str, str | None]:
        body = usage_body(session, **dict(tokens or {}))
        body.update(fields)
        return self.meter.status("usage", body, device)

    def runtime(
        self,
        session: str,
        ms: int = 30_000,
        *,
        device: Actor | None = None,
        valid: bool = True,
        extra: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> tuple[str, str | None]:
        end = self.hub.clock()
        body: Json = {
            "session_id": session,
            "vm_id": "vm-0000abcd",
            "vm_kind": "session",
            "size": "session",
            "from": format_time(end - timedelta(milliseconds=ms)),
            "to": format_time(end),
            "runtime_ms": ms,
        }
        body.update(fields)
        body.update(extra or {})
        if not valid:
            response = self.meter.push("runtime", body, device, extensions={"contract": False})
            assert response.status_code == 200, response.text
            result = response.json()["results"][0]
            return result["status"], result.get("error")
        return self.meter.status("runtime", body, device)
