"""Reconcile steps of the GitHub area:

- `github_revoke` (order 6, before any credential of the run is fetched or purged): registers the revocation of a
  disconnected GitHub token (`github.revoke`, KOR-40) once per daemon;
- `github_selection` (order 45: after the desired state was read and the logs and wraps were verified, before the
  sessions start): keeps the GitHub identity choices and defaults of the desired state in the store
  (`selection.store`, KOR-47, ADR-076 section 3). Without a desired state (pending device, hub not reached) the
  stored answer stays.
"""

from __future__ import annotations

from agent_runner.github import selection
from agent_runner.github.revoke import subscribe
from agent_runner.orgs.db import session
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    subscribe(run.context)


async def selection_step(run: Run) -> None:
    if run.desired is None:
        return
    async with session(run.pool) as conn, conn.transaction():
        await selection.store(conn, run.desired)


STEPS = [ReconcileStep(6, "github_revoke", step), ReconcileStep(45, "github_selection", selection_step)]
