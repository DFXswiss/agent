"""The hint `session.orchestrator` (content-free, 05-runner.md section 17.1) wakes the reconcile run, whose desired
state names the orchestrator work."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.reconcile.hints import wake

HANDLERS: dict[str, Callable[..., Any]] = {"session.orchestrator": wake}
