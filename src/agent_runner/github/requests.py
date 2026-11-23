"""Device-request handlers of the GitHub area (`<area>/requests.py:HANDLERS`, ADR-027 section 4)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.github.device_flow import connect

HANDLERS: dict[str, Callable[..., Any]] = {"github.connect": connect}
