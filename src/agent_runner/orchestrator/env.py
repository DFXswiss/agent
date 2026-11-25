"""Collaborators of the orchestrator area, bundled so the tests inject every one of them (07-testing.md section 1).

`OrchestratorEnv` holds the session area's environment (store, VMs and guest clients, verified logs, device kind,
log, clocks), the signed hub client, the device signing key, this device's hub (origin and pinned response key, the
only hub the relay forwards to), the platform root key and the HTTP transport of the relay (None: the network).
`orchestrator_for(context)` builds the production instance once per daemon context (None while the device is not
enrolled).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.sessions.env import SessionEnv
from agent_runner.sync.http import hub_origin

ATTRIBUTE = "orchestrator_env"
RELAY_TIMEOUT_S = 35.0
"""Limit of one relayed request (below the guest's wait of 40 s, so the tool gets the hub's answer or a refusal)."""


class Hub(Protocol):
    """The part of `sync.http.HubClient` this area uses."""

    async def request(self, method: str, target: str, body: object | None = None) -> dict[str, object]: ...


@dataclass
class OrchestratorEnv:
    sessions: SessionEnv
    hub: Hub
    signing_key: Ed25519PrivateKey
    hub_url: str
    """The origin of this device's hub (`hub_origin(...).base_url`)."""
    hub_key: str
    """b64u of the hub's pinned response key (`device.json`)."""
    platform_root_key: str | None
    transport: httpx.AsyncBaseTransport | None = None
    timeout: float = RELAY_TIMEOUT_S
    relays: Any = field(default=None)
    """The background relays of the daemon (`journal.Relays`, created on first use)."""

    @property
    def db(self) -> Any:
        return self.sessions.db

    @property
    def book(self) -> Any:
        return self.sessions.book

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.sessions.wall(), UTC)

    def line(self, text: str) -> None:
        self.sessions.log(f"runner.orchestrator {text}")


def orchestrator_for(context: Any) -> OrchestratorEnv | None:
    """The orchestrator environment of a daemon; None while the device is not enrolled (or without a daemon)."""
    if context is None:
        return None
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, OrchestratorEnv):
        return found
    from agent_runner.sessions.service import service_for  # noqa: PLC0415 - the service imports the extension points
    from agent_runner.sync.runtime import runtime  # noqa: PLC0415

    service = service_for(context)
    sync = runtime(context)
    if service is None or sync is None:
        return None
    device = context.identity.device
    built = OrchestratorEnv(
        sessions=service.env,
        hub=sync.client,
        signing_key=context.identity.keys.signing_key(),
        hub_url=hub_origin(device.hub_url).base_url,
        hub_key=device.hub_key,
        platform_root_key=context.config.platform_root_key,
    )
    return install(context, built)


def install(context: Any, env: OrchestratorEnv) -> OrchestratorEnv:
    setattr(context, ATTRIBUTE, env)
    return env
