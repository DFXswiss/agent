"""U-RUN-sync (lease): the last verified hub answer and the failover time of the desired state decide whether the work
VM tools with internet egress may run (ADR-086 section 4, 05-runner.md section 6.6)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.sync import lease
from agent_runner.sync.http import HubRefusal, HubTransportError

from .conftest import Harness, run


@pytest.mark.parametrize(
    ("desired", "expected"),
    [
        (None, 300),
        ({}, 300),
        ({"failover_seconds": 60}, 60),
        ({"failover_seconds": 1}, 1),
        ({"failover_seconds": 86400}, 86400),
        ({"failover_seconds": 0}, 300),
        ({"failover_seconds": 86401}, 300),
        ({"failover_seconds": True}, 300),
        ({"failover_seconds": 60.0}, 300),
    ],
)
def test_the_failover_time_of_the_desired_state(desired: Any, expected: int) -> None:
    assert lease.failover_seconds(desired) == expected


def test_the_lease_is_held_within_the_failover_time_of_the_last_answer() -> None:
    client: Any = SimpleNamespace(contact=1000.0)
    desired = {"failover_seconds": 60}
    assert lease.held(client, desired, wall=lambda: 1060.0) is True
    assert lease.held(client, desired, wall=lambda: 1060.5) is False
    assert lease.held(client, None, wall=lambda: 1300.0) is True  # the default before the first state
    assert lease.held(client, None, wall=lambda: 1300.5) is False
    assert lease.held(client, desired, wall=lambda: 999.0) is False  # the clock went back: no contact
    assert lease.held(None, desired, wall=lambda: 1e9) is True  # not enrolled: nothing to lose


def test_only_a_verified_answer_counts_as_hub_contact(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            client = env.sync.client
            assert client.contact == env.wall.now  # wall clock: the creation counts until the first answer
            client.contact = -1.0
            env.hub.tamper.append("unsigned")
            with pytest.raises(HubTransportError):
                await client.request("GET", "/v1/device/state")
            assert client.contact == -1.0
            env.hub.scripted.append((403, {"error": "forbidden"}, {}))
            with pytest.raises(HubRefusal):
                await client.request("GET", "/v1/device/state")
            assert client.contact > 0  # a verified refusal is an answer of the hub
            client.contact = -1.0
            await client.request("GET", "/v1/device/state")
            assert client.contact > 0

    run(body)
