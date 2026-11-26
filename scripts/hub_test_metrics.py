"""Monitoring data for the device double of the web suite (WEB-12): VMs, metric samples, usage and rollups.

The runner's sampler (RUN-15) is not part of the web Playwright suite. `scripts/hub_test_director.py` plays it with
the routes below, signed by the device and pushed through the real outbox (`POST /v1/device/outbox`, HUB-10: the hub
checks every item as it would the runner's): `vm_state` items (which also link the device's open sessions of the
workgroup to the VM), `metrics` items and `usage` items. Items continue the device's stream position
(`hub.device_stream.last_origin_seq`, as `scripts/hub_test_operations.py` does, so both can push for the same device)
and a device pushes at most every `PUSH_INTERVAL` seconds (the hub allows one push per 250 ms and device).
Rollups older than the raw retention have no ingestion path (the hub builds them from raw samples, HUB-16), so they are
written straight into `hub.metric_rollup`.

- `POST /devices/<id>/vms {"wg_id": str | null, "state": str}` -> `{"vm_id"}` (`"session_id"` instead of `wg_id`: the
  workgroup of that session)
- `POST /devices/<id>/metrics {"samples": [{"entity_kind", "entity_id", "age_s"?, <values>?}]}` (`age_s`: seconds
  before now; missing values are typical figures, so a spec names only what it checks)
- `POST /devices/<id>/usage {"session_id", <usage fields>?, "subscription_id"?, "triggered_by"?}` (the last two as the
  runner sends them: the subscription record that served the requests and the person behind them, ADR-100)
- `POST /devices/<id>/rollups {"rollups": [{"entity_kind", "entity_id", "age_h", <field>_avg?, ...}]}`
- `POST /devices/<id>/quotas {"owner_id", "windows": [{"window", "used_percent", "resets_in_s"?, "severity"?}]}`: the
  subscription windows of an owner as the polling device reports them (`quota` item; the hub accepts it only from the
  device that polls the owner, WEB-10)
- `POST /devices/<id>/audit {"session_id", "action", "detail"}`: an outbox audit row of a session the device runs (for
  example `session.model_switched`, ADR-081 section 5: the hub stores the row and notifies the task's owner)

Only loopback, only for tests.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_hub.crypto.encoding import format_time
from agent_hub.device.keys import key_id_of

Json = dict[str, Any]
SignedHeaders = Callable[[str, Callable[[bytes], bytes], str, str, bytes], dict[str, str]]
ROUTE_RE = re.compile(r"^/devices/([0-9a-f-]{36})/(vms|metrics|usage|rollups|quotas|audit)$")
GIB = 1024**3
SAMPLE_DEFAULTS: Json = {
    "cpu_ratio": 0.5,
    "mem_used_bytes": 2 * GIB,
    "mem_limit_bytes": 8 * GIB,
    "disk_used_bytes": 10 * GIB,
    "disk_limit_bytes": 100 * GIB,
    "net_rx_bytes": 1_000_000,
    "net_tx_bytes": 500_000,
    "uptime_seconds": 3600,
    "reachable": True,
    "state": "ready",
    "free_vm_slots": None,
    "free_mem_bytes": None,
}
USAGE_DEFAULTS: dict[str, int] = {
    "requests": 1,
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "thinking_tokens": 0,
    "cost_micro_usd": 12_500,
}
PUSH_INTERVAL = 0.3
ROLLUP_GAUGES = ("cpu_ratio", "mem_used_bytes", "mem_limit_bytes", "disk_used_bytes", "disk_limit_bytes")


def now() -> datetime:
    return datetime.now(UTC)


class MetricsDouble:
    """The monitoring side of the director's devices (module docstring)."""

    def __init__(self, director: Any, signed_headers: SignedHeaders) -> None:
        self.director = director
        self.signed_headers = signed_headers
        self.lock = threading.Lock()
        self.pushed_at: dict[str, float] = {}

    def push(self, device_id: str, items: list[tuple[str, Json]]) -> None:
        """Pushes `(item_kind, body)` items as the device; every item must be accepted."""
        identity = self.director.identities[device_id]
        with self.lock:
            with self.director.pool.connection() as connection:
                row = connection.execute(
                    "SELECT last_origin_seq FROM hub.device_stream WHERE device_id = %s", (device_id,)
                ).fetchone()
            first = (0 if row is None else int(row[0])) + 1
            numbered = [
                {"origin_seq": first + index, "item_kind": kind, "body": body}
                for index, (kind, body) in enumerate(items)
            ]
            raw = json.dumps({"items": numbered}).encode("utf-8")
            url = self.director.hub + "/v1/device/outbox"
            for _attempt in range(20):
                time.sleep(max(0.0, self.pushed_at.get(device_id, 0.0) + PUSH_INTERVAL - time.monotonic()))
                headers = self.signed_headers(key_id_of(device_id), identity.sig.sign, "POST", url, raw)
                answer = self.director.http.post(url, content=raw, headers=headers)
                self.pushed_at[device_id] = time.monotonic()
                if answer.status_code != 429:
                    break
            if answer.status_code != 200:
                raise RuntimeError(f"outbox refused: {answer.text}")
        refused = [result for result in answer.json()["results"] if result["status"] not in ("ok", "replayed")]
        if refused:
            raise RuntimeError(f"outbox items refused: {refused}")

    def vm(self, device_id: str, body: Json) -> str:
        vm_id = "vm-" + secrets.token_hex(4)
        wg_id = body.get("wg_id")
        if "session_id" in body:
            with self.director.pool.connection() as connection:
                row = connection.execute(
                    "SELECT wg_id FROM hub.session WHERE id = %s", (body["session_id"],)
                ).fetchone()
            wg_id = None if row is None else str(row[0])
        state = {
            "vm_id": vm_id,
            "wg_id": wg_id,
            "state": str(body.get("state", "running")),
            "golden_version": "0.1.0",
            "cpus": 4,
            "memory_mib": 8192,
            "disk_gib": 100,
            "at": format_time(now()),
        }
        self.push(device_id, [("vm_state", state)])
        return vm_id

    def metrics(self, device_id: str, body: Json) -> None:
        moment = now()
        samples = []
        for given in body.get("samples", []):
            sample = {**SAMPLE_DEFAULTS, **{key: value for key, value in given.items() if key in SAMPLE_DEFAULTS}}
            sample["entity_kind"] = given["entity_kind"]
            sample["entity_id"] = given["entity_id"]
            sample["ts"] = format_time(moment - timedelta(seconds=float(given.get("age_s", 0))))
            samples.append(sample)
        self.push(device_id, [("metrics", {"samples": samples})])

    def usage(self, device_id: str, body: Json) -> None:
        usage = {
            "session_id": body["session_id"],
            "delta": {key: int(body.get(key, value)) for key, value in USAGE_DEFAULTS.items()},
            "cost_basis": str(body.get("cost_basis", "list_price")),
            "context_used_tokens": int(body.get("context_used_tokens", 50_000)),
            "context_window_tokens": int(body.get("context_window_tokens", 200_000)),
            "last_provider": body.get("last_provider", "anthropic"),
        }
        for name in ("subscription_id", "triggered_by"):
            if name in body:
                usage[name] = body[name]
        self.push(device_id, [("usage", usage)])

    def quotas(self, device_id: str, body: Json) -> None:
        moment = now()
        windows = [
            {
                "window": str(given["window"]),
                "used_percent": float(given["used_percent"]),
                "resets_at": None
                if given.get("resets_in_s") is None
                else format_time(moment + timedelta(seconds=float(given["resets_in_s"]))),
                "severity": given.get("severity"),
            }
            for given in body.get("windows", [])
        ]
        quota = {"owner_id": str(body["owner_id"]), "windows": windows, "fetched_at": format_time(moment)}
        self.push(device_id, [("quota", quota)])

    def rollups(self, device_id: str, body: Json) -> None:
        bucket = int(now().timestamp()) // 300 * 300
        rows = []
        for given in body.get("rollups", []):
            start = datetime.fromtimestamp(bucket - int(float(given["age_h"]) * 3600) // 300 * 300, UTC)
            gauges = []
            for name in ROLLUP_GAUGES:
                value = given.get(f"{name}_avg", SAMPLE_DEFAULTS[name])
                gauges.extend([value, value])
            rows.append(
                (given["entity_kind"], given["entity_id"], start, device_id, int(given.get("samples", 20)), *gauges)
            )
        columns = ", ".join(f"{name}_avg, {name}_max" for name in ROLLUP_GAUGES)
        with self.director.pool.connection() as connection, connection.transaction():
            connection.cursor().executemany(
                "INSERT INTO hub.metric_rollup (entity_kind, entity_id, ts, device_id, samples, "
                f"{columns}, reachable, state) VALUES ({', '.join(['%s'] * (5 + 2 * len(ROLLUP_GAUGES)))}, true,"
                " 'ready') ON CONFLICT DO NOTHING",
                rows,
            )

    def audit(self, device_id: str, body: Json) -> None:
        with self.director.pool.connection() as connection:
            row = connection.execute("SELECT task_id FROM hub.session WHERE id = %s", (body["session_id"],)).fetchone()
        task_id = None if row is None else str(row[0])
        item = {
            "action": str(body["action"]),
            "result": "ok",
            "task_id": task_id,
            "session_id": body["session_id"],
            "target_kind": "session",
            "target_id": body["session_id"],
            "detail": body.get("detail", {}),
        }
        self.push(device_id, [("audit", item)])

    def route(self, path: str, body: Json) -> Json | None:
        """The answer of a monitoring route, None for another path."""
        match = ROUTE_RE.match(path)
        if match is None:
            return None
        device_id, what = match.group(1), match.group(2)
        if what == "vms":
            return {"vm_id": self.vm(device_id, body)}
        getattr(self, what)(device_id, body)
        return {}
