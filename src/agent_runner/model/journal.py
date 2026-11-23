"""Journal handlers of the model area (`<area>/journal.py:HANDLERS`, ADR-027 section 4): the gateway's `usage`
records (`model.usage`, 05-runner.md section 10.4)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.model import usage

HANDLERS: dict[str, Callable[..., Any]] = {"usage": usage.handle}
