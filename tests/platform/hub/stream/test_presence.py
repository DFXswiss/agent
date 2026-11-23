"""U-HUB-stream (presence): the online window, the expiry job of crashed processes and its signal and audit rows
(02-data-model.md section 2.1 `device_presence`, 03-api.md section 12)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_hub.registry import JOBS, concat_sequences
from agent_hub.stream import presence
from agent_hub.stream.jobs import expire_presence
from tests.platform.hub.conftest import HubHarness, uuid7

Emitted = list[tuple[str, dict[str, Any]]]


def _insert(hub: HubHarness, device: Any, seconds_ago: float) -> None:
    moment = hub.clock() - timedelta(seconds=seconds_ago)
    with hub.pool.connection() as connection:
        connection.execute(
            "INSERT INTO hub.device_presence (device_id, hub_instance, connected_at, last_heartbeat_at)"
            " VALUES (%s, 'gone', %s, %s)",
            (device, moment, moment),
        )


def test_online_window_and_expiry(hub: HubHarness, emitted: Emitted) -> None:
    fresh = hub.keys.device().principal.principal_id
    stale = hub.keys.device().principal.principal_id
    assert fresh is not None
    assert stale is not None
    _insert(hub, fresh, 29)
    _insert(hub, stale, 30)
    window = hub.config.device_online_seconds
    with hub.pool.connection() as connection:
        assert presence.online_devices(connection, [fresh, stale, str(fresh)], hub.clock(), window) == {fresh}
        assert not presence.is_online(connection, stale, hub.clock(), window)
    context = hub.app.state.hub
    assert expire_presence(context) == [str(stale)]
    assert emitted == [("device.presence", {"device_id": stale, "online": 0})]
    rows = hub.app.state.hub.pool.connection
    with rows() as connection:
        remaining = connection.execute("SELECT device_id FROM hub.device_presence").fetchall()
        audit = connection.execute("SELECT action, device_id, detail FROM hub.audit ORDER BY seq").fetchall()
    assert remaining == [(fresh,)]
    assert audit == [("device.disconnect", stale, {"channel": "ws"})]
    hub.clock.advance(1)
    assert expire_presence(context) == [str(fresh)]


def test_connect_refuses_devices_that_are_not_active(hub: HubHarness) -> None:
    for state in ("pending_certification", "decertified", "removed"):
        device = hub.keys.device(state=state).principal.principal_id
        assert device is not None
        with hub.pool.connection() as connection:
            assert presence.connect(connection, device, "instance", hub.clock(), 30) is None
    with hub.pool.connection() as connection:
        assert presence.connect(connection, uuid7(hub.clock()), "instance", hub.clock(), 30) is None


def test_the_expiry_job_is_registered() -> None:
    names = [job.name for job in concat_sequences(JOBS)]
    assert "stream.presence_expire" in names
