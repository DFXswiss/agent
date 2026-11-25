"""Reconcile step of the profiles area: `agent_profiles` (order 46: after the desired state was read and the logs and
wraps were verified, before the sessions start) keeps the section `agent_profiles` of the desired state in the store
(`state.store`, ADR-090 section 3). Without a desired state (pending device, hub not reached) the stored one stays."""

from __future__ import annotations

from agent_runner.orgs.db import session
from agent_runner.profiles import state
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    if run.desired is None:
        return
    async with session(run.pool) as conn, conn.transaction():
        await state.store(conn, run.desired)


STEPS = [ReconcileStep(46, "agent_profiles", step)]
