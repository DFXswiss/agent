"""Daemon tasks of the billing area (ADR-027 section 4 `<area>/tasks.py:TASKS`): the VM runtime meter
(`agent_runner.billing.meter`, KOR-31)."""

from __future__ import annotations

from agent_runner.billing.meter import run
from agent_runner.registry import TaskSpec

TASKS = [TaskSpec("billing_meter", run)]
