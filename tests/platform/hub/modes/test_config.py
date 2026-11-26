"""U-HUB-modes: the setting `AGENT_HUB_MODES_TEST` (the hub offers the catalogue's test modes; KOR-75)."""

from __future__ import annotations

import pytest

from agent_hub.config import ConfigError, load_config
from tests.platform.hub.conftest import hub_environ


def test_test_modes_are_off_unless_configured() -> None:
    assert load_config(hub_environ()).modes_test is False
    assert load_config(hub_environ(AGENT_HUB_MODES_TEST="0")).modes_test is False
    assert load_config(hub_environ(AGENT_HUB_MODES_TEST="1")).modes_test is True
    with pytest.raises(ConfigError, match="AGENT_HUB_MODES_TEST must be 0 or 1"):
        load_config(hub_environ(AGENT_HUB_MODES_TEST="yes"))
