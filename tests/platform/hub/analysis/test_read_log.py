"""U-HUB-analysis (read log): every read of `GET /v1/device/workgroups/{wg}/usage|audit|operations|metrics` is a hub
read-log row `read.device.workgroup.<resource>` of the device, naming the workgroup read, its refusals included
(ADR-097 section 3)."""

from __future__ import annotations

from typing import Any

import pytest

from agent_hub.crypto.encoding import format_time
from tests.platform.hub.analysis.support import Reads
from tests.platform.hub.device.support import Fleet
from tests.platform.hub.orgs.support import World


@pytest.fixture
def reads(world: World, fleet: Fleet) -> Reads:
    return Reads.build(world, fleet)


def _logged(reads: Reads) -> list[tuple[Any, ...]]:
    return reads.scene.world.rows(
        "SELECT action, actor_kind, actor_id::text, device_id::text, wg_id::text, result FROM hub.audit"
        " WHERE action LIKE %s ORDER BY seq",
        "read.device.workgroup.%",
    )


def test_every_analysis_read_is_logged_with_the_workgroup(reads: Reads) -> None:
    session = reads.session(reads.task(reads.ago(hours=1)), reads.ago(hours=1))
    since = format_time(reads.ago(minutes=10))
    reads.ok("usage", since=since)
    reads.ok("audit", group_by="action")
    reads.ok("operations", state="failed")
    reads.ok("metrics", entity_kind="session", entity_id=session, metric="cpu_ratio", since=since, step="60")
    assert reads.get("usage", wg=reads.other_wg, since=since).status_code == 404
    device = reads.scene.device.id
    assert _logged(reads) == [
        (f"read.device.workgroup.{resource}", "device", device, device, wg, result)
        for resource, wg, result in (
            ("usage", reads.wg, "ok"),
            ("audit", reads.wg, "ok"),
            ("operations", reads.wg, "ok"),
            ("metrics", reads.wg, "ok"),
            ("usage", reads.other_wg, "denied"),
        )
    ]
