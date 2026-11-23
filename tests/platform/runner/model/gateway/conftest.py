"""Fixtures of the gateway injection suite: the guest control and API processes of the guest suites (GST-01) and the
model double of the gateway suite (GST-04), reused by import."""

from __future__ import annotations

from typing import Any

import pytest

from agent_guest import sessions
from agent_guest.control import ControlContext
from tests.platform.guest.conftest import (  # noqa: F401  (fixtures)
    api_port,
    api_server,
    clock,
    context,
    contracts_dir,
    control,
    control_client,
    guest_config,
    guest_env,
    journal,
    own_group,
    schemas,
    session,
    short_tmp,
)
from tests.platform.guest.gateway.conftest import api, start_double  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def started_agents(monkeypatch: pytest.MonkeyPatch, session: Any) -> list[str]:  # noqa: F811
    """The working session of the guest suite exists; the agent start the provisioning asks GST-05 for is recorded
    (the session manager needs Linux and root)."""
    calls: list[str] = []

    def record(_context: ControlContext, session_id: str) -> bool:
        calls.append(session_id)
        return True

    monkeypatch.setattr(sessions, "credential_provisioned", record)
    return calls
