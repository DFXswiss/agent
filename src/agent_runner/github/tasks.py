"""Daemon tasks of the GitHub area (ADR-027 section 4 `<area>/tasks.py:TASKS`): the audit of the operator's approved
users at each start (`github.operator_audit`, KOR-47); it ends after one run."""

from __future__ import annotations

from agent_runner.github.operator_audit import audit
from agent_runner.registry import TaskSpec

TASKS = [TaskSpec("github_operator_users", audit)]
