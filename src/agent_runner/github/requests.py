"""Device-request handlers of the GitHub area (`<area>/requests.py:HANDLERS`, ADR-027 section 4): `github.connect`
(`github.device_flow`) and `github.pull_status` (`github.pull_status`, KOR-48)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.github.device_flow import connect
from agent_runner.github.pull_status import pull_status

HANDLERS: dict[str, Callable[..., Any]] = {"github.connect": connect, "github.pull_status": pull_status}
