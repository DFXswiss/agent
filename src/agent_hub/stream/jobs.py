"""Background job of the live channels (ADR-027 section 4): devices whose heartbeat stopped go offline
(`agent_hub.stream.presence.expire`, then the signal `device.presence` with `online=0`)."""

from __future__ import annotations

from agent_hub.http.context import HubContext
from agent_hub.jobs import Job
from agent_hub.stream import presence

EXPIRE_INTERVAL_SECONDS = 5.0


def expire_presence(context: HubContext) -> list[str]:
    """One pass: expire stale presence rows and announce the devices that went offline."""
    with context.pool.connection() as connection, connection.transaction():
        devices = presence.expire(connection, context.clock(), context.config.device_online_seconds)
    for device in devices:
        presence.announce_presence(device, False)
    return [str(device) for device in devices]


JOBS = [Job("stream.presence_expire", EXPIRE_INTERVAL_SECONDS, expire_presence)]
