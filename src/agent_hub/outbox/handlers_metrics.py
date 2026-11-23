"""Outbox item `metrics` (03-api.md section 6.1, 02-data-model.md section 2.8): raw `metric_sample` rows.

Every sample must describe the device itself, one of its VMs (`vm` row of the device) or a session running on it
(`device:session`); a foreign entity is `403 forbidden`, an unknown VM or session `404 not_found`, and one bad sample
refuses the whole item. A sample whose key (`entity_kind`, `entity_id`, `ts`) is already stored is ignored (the same
sample reported twice). Rollups and pruning belong to the monitoring area (HUB-16).
"""

from __future__ import annotations

from agent_hub.errors import HubError
from agent_hub.outbox.base import ItemContext, moment, session_on_device
from agent_hub.outbox.bodies import MetricSample, MetricsBody

COLUMNS = (
    "cpu_ratio",
    "mem_used_bytes",
    "mem_limit_bytes",
    "disk_used_bytes",
    "disk_limit_bytes",
    "net_rx_bytes",
    "net_tx_bytes",
    "uptime_seconds",
    "reachable",
    "state",
    "free_vm_slots",
    "free_mem_bytes",
)
INSERT = (
    f"INSERT INTO hub.metric_sample (entity_kind, entity_id, ts, device_id, {', '.join(COLUMNS)})"
    f" VALUES ({', '.join(['%s'] * (4 + len(COLUMNS)))}) ON CONFLICT DO NOTHING"
)


def check_entity(item: ItemContext, sample: MetricSample) -> None:
    """The sampled entity belongs to the pushing device (module docstring)."""
    if sample.entity_kind == "device":
        if sample.entity_id != str(item.device):
            raise HubError("forbidden")
    elif sample.entity_kind == "vm":
        row = item.connection.execute("SELECT device_id FROM hub.vm WHERE id = %s", (sample.entity_id,)).fetchone()
        if row is None:
            raise HubError("not_found")
        if row[0] != item.device:
            raise HubError("forbidden")
    else:
        session_on_device(item, sample.entity_id)


def handle(item: ItemContext, body: MetricsBody) -> None:
    """Insert the samples."""
    checked: set[tuple[str, str]] = set()
    rows = []
    for sample in body.samples:
        if (sample.entity_kind, sample.entity_id) not in checked:
            check_entity(item, sample)
            checked.add((sample.entity_kind, sample.entity_id))
        rows.append(
            (
                sample.entity_kind,
                sample.entity_id,
                moment(sample.ts),
                item.device,
                *(getattr(sample, name) for name in COLUMNS),
            )
        )
    with item.connection.cursor() as cursor:
        cursor.executemany(INSERT, rows)
