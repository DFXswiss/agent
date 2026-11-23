"""U-HUB-device-api (hints): `reconcile {}` to the devices of a membership-log change (03-api.md section 6.2)."""

from __future__ import annotations

from concurrent import futures
from types import SimpleNamespace
from uuid import UUID

import pytest

from agent_hub import signals
from agent_hub.device import jobs
from agent_hub.device import signals as device_signals
from agent_hub.registry import SIGNAL_SUBSCRIBERS, discover
from agent_hub.stream.publish import MemoryPublisher
from tests.platform.hub.device.support import Fleet, org_device_scene
from tests.platform.hub.orgs.support import World


def reconciled(published: MemoryPublisher) -> list[str]:
    return sorted(str(item.audience.id) for item in published.items() if item.frame == "reconcile")


def test_subscriber_is_registered() -> None:
    found = dict(discover(SIGNAL_SUBSCRIBERS))["device"]
    assert list(found["log.appended"]) == [device_signals.on_log_appended]


def test_no_hints_before_a_device_request(world: World, fleet: Fleet, published: MemoryPublisher) -> None:
    draft = world.public_org()
    fleet.org_device(draft)
    assert reconciled(published) == []
    device_signals.on_log_appended("log.appended", {"org_id": UUID(draft.id), "seq": 0})
    closed = SimpleNamespace(pool=SimpleNamespace(closed=True))
    device_signals.bind(closed)
    device_signals.on_log_appended("log.appended", {"org_id": UUID(draft.id), "seq": 0})
    device_signals.wait()
    assert reconciled(published) == []


def test_failures_are_logged(caplog: pytest.LogCaptureFixture, published: MemoryPublisher) -> None:
    def broken() -> None:
        raise RuntimeError("no connection")

    context = SimpleNamespace(pool=SimpleNamespace(closed=False, connection=broken))
    device_signals.bind(context)
    device_signals.on_log_appended("log.appended", {"org_id": UUID(int=1), "seq": 0})
    device_signals.wait()
    assert reconciled(published) == []
    assert "reconcile hints" in caplog.text


def test_job_binds_the_context() -> None:
    (job,) = jobs.JOBS
    context = SimpleNamespace(pool=None)
    job.run(context)
    assert device_signals._context["hub"] is context


def test_enrollment_reconciles_the_org_devices(world: World, fleet: Fleet, published: MemoryPublisher) -> None:
    draft = world.public_org()
    first = fleet.org_device(draft)
    assert fleet.client(first).get("/v1/device/state").status_code == 200
    published.clear()
    second = fleet.org_device(draft)
    device_signals.wait()
    assert reconciled(published) == sorted([first.id, second.id])


def test_removed_device_is_reconciled(world: World, fleet: Fleet, published: MemoryPublisher) -> None:
    scene = org_device_scene(world)
    draft, admin = scene.draft, scene.admin
    assert fleet.client(scene.other_device).get("/v1/device/state").status_code == 200
    draft.add("remove", admin, subject=scene.device.id)
    draft.add("rotate", admin, scope="org", scope_id=scene.org, epoch=2)
    draft.add("rotate", admin, scope="wg", scope_id=scene.wg, epoch=2)
    readers = [admin, scene.writer, scene.reader, scene.other_device]
    wraps = draft.wraps(admin, "org", scene.org, 2, readers) + draft.wraps(admin, "wg", scene.wg, 2, readers)
    published.clear()
    response = draft.post(admin, wraps)
    assert response.status_code == 200, response.text
    device_signals.wait()
    assert reconciled(published) == sorted([scene.device.id, scene.other_device.id])
    assert device_signals.on_log_appended in signals.subscribers()["log.appended"]


def test_finished_reconciles_are_not_kept() -> None:
    """A-13: one future per log append must not pile up in a long-running hub (finished ones are dropped)."""
    device_signals.bind(SimpleNamespace(pool=SimpleNamespace(closed=True)))
    for seq in range(50):
        device_signals.on_log_appended("log.appended", {"org_id": UUID(int=1), "seq": seq})
    futures.wait(list(device_signals._pending))
    device_signals.on_log_appended("log.appended", {"org_id": UUID(int=1), "seq": 50})
    assert len(device_signals._pending) == 1
    device_signals.wait()
    assert device_signals._pending == []
