"""U-HUB-device (ADR-060 decision 6): the `Agent-Features` header of the live channel (agent_hub.device.features):
parsing, recording on connect (replaced, never merged) and the devices that lack a feature."""

from __future__ import annotations

import pytest

from agent_hub.device import features
from agent_hub.stream import presence
from tests.platform.hub.conftest import HubHarness


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()),
        ("", ()),
        ("log.admit", ("log.admit",)),
        (" log.admit , beta_1,log.admit ", ("beta_1", "log.admit")),
        ("Log.Admit,-x,.y,a b,é", ()),
        ("a" * 32 + "," + "b" * 33, ("a" * 32,)),
        (",".join(f"f{i:02d}" for i in range(20)), tuple(f"f{i:02d}" for i in range(features.MAX_FEATURES))),
    ],
)
def test_parse_features(value: str | None, expected: tuple[str, ...]) -> None:
    assert features.parse_features(value) == expected


def test_connect_records_the_announced_features(hub: HubHarness) -> None:
    first = hub.keys.device().principal.principal_id
    second = hub.keys.device().principal.principal_id
    assert first is not None
    assert second is not None
    window = hub.config.device_online_seconds
    with hub.pool.connection() as connection:
        assert presence.connect(connection, first, "a", hub.clock(), window, features=("log.admit",)) is False
        assert features.outdated_devices(connection, [str(first), str(second), str(second)]) == [str(second)]
        assert features.outdated_devices(connection, []) == []
        assert features.outdated_devices(connection, [str(first)], "other") == [str(first)]
        presence.connect(connection, first, "a", hub.clock(), window)
        assert features.outdated_devices(connection, [str(first)]) == [str(first)]
        features.record_features(connection, second, ["log.admit"])
        features.record_features(connection, second, ["log.admit"])  # unchanged: no write
        assert features.outdated_devices(connection, [str(second)]) == []
