"""`metrics` frames of the browser live channel (03-api.md section 5, 08-monitoring.md section 6).

Every `FRAME_SECONDS` (15 s, the sampling interval) the job `monitoring.metrics_frames` publishes the newest sample of
every entity sampled within the last `FRESH_SECONDS` through `agent_hub.stream.publish` to the browsers of the orgs that
see it; the stream (HUB-07) delivers `metrics` frames only to streams opened with `monitoring=1` and applies the
workgroup filter. The rights are those of the overview (`agent_hub.monitoring.read`):

- device samples go to every org the device is an active member of, in frames without a workgroup (a shared
  platform device thus reaches each of its orgs, but only with the device sample);
- VM and session samples go to the org of their workgroup in frames published with `wg_id=` that workgroup, so only
  readers of the workgroup receive them (a frame with VM or session samples and no `wg_id=` would reach nobody);
- work VM samples (KOR-29) go like the samples of the session their newest sample names.

A frame holds `{samples:[{entity_kind, entity_id, sample}]}` (`contracts/ws/sse/metrics.json`) and is split so that
its JSON stays within `FRAME_BYTES`: the bus between hub processes carries a frame in one PostgreSQL notification
(< 7900 bytes, `agent_hub.stream.bus`), a larger one would turn into a resync. Each hub process runs the job; with
several processes a stream can receive a sample more than once, which the client tolerates (it keeps the newest
sample per entity).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.monitoring.read import SAMPLE_COLUMNS, sample_json
from agent_hub.stream.publish import Audience, publish

Connection = psycopg.Connection[Any]
FRAME_SECONDS = 15.0
FRESH_SECONDS = 30
FRAME_BYTES = 7000
Item = dict[str, Any]
Target = tuple[UUID, UUID | None]
"""`(org, workgroup)`: the audience of a frame and its workgroup restriction (None for device samples)."""


def _size(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=True))


def frames_of(items: Iterable[Item], limit: int = FRAME_BYTES) -> list[dict[str, Any]]:
    """The data of the frames that carry `items` in order, each within `limit` bytes of JSON."""
    frames: list[dict[str, Any]] = []
    current: list[Item] = []
    size = _size({"samples": []})
    for item in items:
        extra = _size(item) + (1 if current else 0)
        if current and size + extra > limit:
            frames.append({"samples": current})
            current, size = [], _size({"samples": []})
            extra = _size(item)
        current.append(item)
        size += extra
    if current:
        frames.append({"samples": current})
    return frames


def fresh_samples(connection: Connection, now: datetime) -> list[tuple[str, str, Item]]:
    """`(entity_kind, entity_id, item)` of the newest sample of every entity sampled within `FRESH_SECONDS`."""
    rows = connection.execute(
        f"SELECT DISTINCT ON (entity_kind, entity_id) entity_kind, entity_id, ts, {', '.join(SAMPLE_COLUMNS)}"
        " FROM hub.metric_sample WHERE ts > %s ORDER BY entity_kind, entity_id, ts DESC",
        (now - timedelta(seconds=FRESH_SECONDS),),
    ).fetchall()
    return [
        (str(row[0]), str(row[1]), {"entity_kind": row[0], "entity_id": row[1], "sample": sample_json(row[2], row[3:])})
        for row in rows
    ]


def _ids(found: list[tuple[str, str, Item]], kind: str) -> list[str]:
    return [entity_id for entity_kind, entity_id, _ in found if entity_kind == kind]


def targets(connection: Connection, found: list[tuple[str, str, Item]]) -> dict[Target, list[Item]]:
    """The samples per `(org, workgroup)` audience (module docstring), in the order of `found`."""
    devices: dict[str, list[UUID]] = defaultdict(list)
    for device_id, org in connection.execute(
        "SELECT principal_id, org_id FROM hub.org_member WHERE principal_id = ANY(%s::uuid[]) AND kind = 'device'"
        " AND removed_seq IS NULL ORDER BY org_id",
        (_ids(found, "device"),),
    ):
        devices[str(device_id)].append(org)
    scoped: dict[tuple[str, str], Target] = {}
    for vm_id, org, wg in connection.execute(
        "SELECT v.id, w.org_id, w.id FROM hub.vm v JOIN hub.wg w ON w.id = v.wg_id WHERE v.id = ANY(%s::text[])",
        (_ids(found, "vm"),),
    ):
        scoped[("vm", str(vm_id))] = (org, wg)
    for session_id, org, wg in connection.execute(
        "SELECT id, org_id, wg_id FROM hub.session WHERE id = ANY(%s::uuid[])", (_ids(found, "session"),)
    ):
        scoped[("session", str(session_id))] = (org, wg)
    for workvm_id, org, wg in connection.execute(
        "SELECT DISTINCT ON (m.entity_id) m.entity_id, s.org_id, s.wg_id FROM hub.metric_sample m"
        " JOIN hub.session s ON s.id = m.session_id WHERE m.entity_kind = 'workvm' AND m.entity_id = ANY(%s::text[])"
        " ORDER BY m.entity_id, m.ts DESC",
        (_ids(found, "workvm"),),
    ):
        scoped[("workvm", str(workvm_id))] = (org, wg)
    out: dict[Target, list[Item]] = defaultdict(list)
    for kind, entity_id, item in found:
        if kind == "device":
            for org in devices.get(entity_id, []):
                out[(org, None)].append(item)
        elif (kind, entity_id) in scoped:
            out[scoped[(kind, entity_id)]].append(item)
    return dict(out)


def publish_metrics(context: Any) -> int:
    """One tick of the job: publish the `metrics` frames (after the reads); the number of frames."""
    with context.pool.connection() as connection:
        found = targets(connection, fresh_samples(connection, context.clock()))
    count = 0
    for (org, wg), items in found.items():
        for data in frames_of(items):
            publish(Audience("org", org), "metrics", data, wg_id=wg)
            count += 1
    return count
