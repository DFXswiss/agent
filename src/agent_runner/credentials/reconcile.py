"""Reconcile step 7, credential changes (05-runner.md sections 2.2 and 2.6, 03-api.md section 6.1): every owner listed
in `credentials_changed` of the desired state is re-read (`identities.refresh`: the legacy kinds of a user and every
cached GitHub identity record of the owner, a user or an organization, KOR-47; the hub lists an owner until each
changed record was read again, deletions as `404`), then its subscription records are listed and re-read
(`subscriptions.sync_owner`, KOR-69: a changed use or a new login of a shared subscription reaches the pool even when
the live hint was missed)."""

from __future__ import annotations

from agent_runner.credentials import subscriptions
from agent_runner.credentials.identities import refresh
from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    desired = run.desired or {}
    listed = wire.array(desired.get("credentials_changed", []), "credentials_changed")
    owners = [wire.uuid_text(item, "credentials_changed") for item in listed]
    for owner in owners:
        await refresh(run.context, owner)
        await subscriptions.sync_owner(run.context, owner)


STEPS = [ReconcileStep(70, "credentials", step)]
