"""Reconcile step `github_revoke` (order 6, before any credential of the run is fetched or purged): registers the
revocation of a disconnected GitHub token (`github.revoke`, KOR-40) once per daemon."""

from __future__ import annotations

from agent_runner.github.revoke import subscribe
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    subscribe(run.context)


STEPS = [ReconcileStep(6, "github_revoke", step)]
