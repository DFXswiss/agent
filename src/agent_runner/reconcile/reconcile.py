"""Reconcile steps 1-3 of this area (05-runner.md section 2.2; order numbers in `reconcile.engine`)."""

from __future__ import annotations

from agent_runner.reconcile import logs, state, wraps
from agent_runner.registry import ReconcileStep

STEPS = [
    ReconcileStep(10, "state", state.fetch_state),
    ReconcileStep(20, "logs", logs.sync_logs),
    ReconcileStep(30, "wraps", wraps.step),
]
