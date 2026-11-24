"""The fault switch of the attack suite (ADR-064 section 3): ``AGENT_GUEST_FAULT_HOOKS`` takes effect only in a
TEST_MODE golden (``test_mode`` true in the golden's ``install.json``); elsewhere the switch does nothing.

* ``500`` - the hooks answer 500 and the production reaction applies: a failing decision hook ends the session
  (``hook_unavailable``), the gateway refuses agent-loop requests outside a confirmed turn.
* ``open`` - the hooks answer 500 and that reaction is suppressed (turn gate open, no session ended): the attack
  suite's mode ``fault`` proves the second layer (sandbox) alone.

The hook handler, the gateway and the control process read the switch only through ``fault_mode``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from agent_guest.config import GuestConfig

FAIL = "500"
OPEN = "open"


@lru_cache(maxsize=8)
def golden_test_mode(path: str) -> bool:
    """``True`` only if the golden's install record exists and says ``"test_mode": true``."""
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("test_mode") is True


def fault_mode(config: GuestConfig) -> Literal["500", "open"] | None:
    """The active fault mode, ``None`` without the switch or outside a TEST_MODE golden."""
    if config.fault_hooks not in (FAIL, OPEN) or not golden_test_mode(config.golden_install_file):
        return None
    return "500" if config.fault_hooks == FAIL else "open"


def guard_suppressed(config: GuestConfig) -> bool:
    """The fail-closed reaction is suppressed (``open`` in a TEST_MODE golden)."""
    return fault_mode(config) == OPEN
