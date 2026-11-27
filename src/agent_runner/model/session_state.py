"""Observer of the guest's `session.state` records (`sessions.extensions` `OBSERVERS`, ADR-032): a turn a retryable
model error ended is recorded for its continuation by the platform (`model.retry`, FIX-02)."""

from __future__ import annotations

from agent_runner.model import retry
from agent_runner.sessions.extensions import StateObserver

OBSERVERS = [StateObserver("model.retry", retry.observe)]
