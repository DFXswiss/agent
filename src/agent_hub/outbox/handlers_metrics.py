"""Outbox item `metrics` (03-api.md section 6.1, 02-data-model.md section 2.8): raw `metric_sample` rows.

Every sample must describe the device itself, one of its VMs (`vm` row of the device) or a session running on it
(`device:session`); a foreign entity is `403 forbidden`, an unknown VM or session `404 not_found`, and one bad sample
refuses the whole item. A work VM sample (KOR-29) names the session the work VM belongs to (`session_id`), which must
run on the device (`device:session`); a sample of a work VM ID whose newest sample has another device or session is
dropped (a device cannot write into the series of a foreign work VM, and an ID that two devices happen to draw - 32
random bits per device - costs only the samples of that work VM, never the whole item).
A sample whose key (`entity_kind`, `entity_id`, `ts`) is already stored is ignored (the same sample reported twice).
Rollups and pruning belong to the monitoring area (HUB-16).
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
    f"INSERT INTO hub.metric_sample (entity_kind, entity_id, ts, device_id, session_id, {', '.join(COLUMNS)})"
    f" VALUES ({', '.join(['%s'] * (5 + len(COLUMNS)))}) ON CONFLICT DO NOTHING"
)


def check_entity(item: ItemContext, sample: MetricSample) -> bool:
    """The sampled entity belongs to the pushing device (module docstring); False: drop the samples of the entity."""
    if sample.entity_kind == "device":
        if sample.entity_id != str(item.device):
            raise HubError("forbidden")
    elif sample.entity_kind == "vm":
        row = item.connection.execute("SELECT device_id FROM hub.vm WHERE id = %s", (sample.entity_id,)).fetchone()
        if row is None:
            raise HubError("not_found")
        if row[0] != item.device:
            raise HubError("forbidden")
    elif sample.entity_kind == "workvm":
        assert sample.session_id is not None  # `MetricSample` requires it for a work VM
        session_on_device(item, sample.session_id)
        row = item.connection.execute(
            "SELECT device_id, session_id FROM hub.metric_sample WHERE entity_kind = 'workvm' AND entity_id = %s"
            " ORDER BY ts DESC LIMIT 1",
            (sample.entity_id,),
        ).fetchone()
        return row is None or (row[0] == item.device and str(row[1]) == sample.session_id)
    else:
        session_on_device(item, sample.entity_id)
    return True


def handle(item: ItemContext, body: MetricsBody) -> None:
    """Insert the samples."""
    checked: dict[tuple[str, str], bool] = {}
    rows = []
    for sample in body.samples:
        key = (sample.entity_kind, sample.entity_id)
        if key not in checked:
            checked[key] = check_entity(item, sample)
        if not checked[key]:
            continue
        rows.append(
            (
                sample.entity_kind,
                sample.entity_id,
                moment(sample.ts),
                item.device,
                sample.session_id,
                *(getattr(sample, name) for name in COLUMNS),
            )
        )
    with item.connection.cursor() as cursor:
        cursor.executemany(INSERT, rows)
