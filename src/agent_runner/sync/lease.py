"""The runner's lease of the work VM tools (ADR-086 section 4; docs/platform/spec/05-runner.md section 6.6).

`workvm_exec` and `workvm_browse` reach the internet from the work VM without a hub registration. A device the hub
cannot reach may have been declared failed and its sessions moved to another device; such a device must not go on
acting outside for the lost sessions. The lease is **held** while the last verified hub answer (`HubClient.contact`,
the client's creation before the first answer) is at most `failover_seconds` old - the value of the last desired
state of an active device (`AGENT_HUB_DEVICE_FAILOVER_SECONDS`), `DEFAULT_FAILOVER_SECONDS` before the first state,
from an older hub that does not send it, or when it is malformed. The age is measured on the wall clock (a monotonic
clock stands still while the host sleeps, so a device that slept for an hour would still hold it); a negative age
(the clock went back) counts as no contact until the next hub answer. Without a hub client (a device that is not
enrolled) there is nothing to lose and the lease is held. Nothing else is stopped: running turns go on (a hub outage
must not stop devices).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.sync.http import HubClient

DEFAULT_FAILOVER_SECONDS = 300
MAX_FAILOVER_SECONDS = 86400


def failover_seconds(desired: Mapping[str, Any] | None) -> int:
    """`failover_seconds` of the desired state (module docstring)."""
    value = None if desired is None else desired.get("failover_seconds")
    if type(value) is int and 1 <= value <= MAX_FAILOVER_SECONDS:
        return value
    return DEFAULT_FAILOVER_SECONDS


def held(client: HubClient | None, desired: Mapping[str, Any] | None, *, wall: Callable[[], float] = time.time) -> bool:
    """Whether the hub answered within the failover time (module docstring)."""
    if client is None:
        return True
    age = wall() - client.contact
    return 0 <= age <= failover_seconds(desired)
